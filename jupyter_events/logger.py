"""
Emit structured, discrete events when various actions happen.
"""
import asyncio
import copy
import inspect
import json
import logging
import warnings
from datetime import datetime
from pathlib import PurePath
from typing import Callable, Union

from pythonjsonlogger import jsonlogger
from traitlets import Dict, Instance, Set, default
from traitlets.config import Config, LoggingConfigurable

from .schema_registry import SchemaRegistry
from .traits import Handlers

# Increment this version when the metadata included with each event
# changes.
EVENTS_METADATA_VERSION = 1


class SchemaNotRegistered(Warning):
    """A warning to raise when an event is given to the logger
    but its schema has not be registered with the EventLogger
    """


class ModifierError(Exception):
    """An exception to raise when a modifier does not
    show the proper signature.
    """


# Only show this warning on the first instance
# of each event type that fails to emit.
warnings.simplefilter("once", SchemaNotRegistered)


class ListenerError(Exception):
    """An exception to raise when a listener does not
    show the proper signature.
    """


class EventLogger(LoggingConfigurable):
    """
    An Event logger for emitting structured events.

    Event schemas must be registered with the
    EventLogger using the `register_schema` or
    `register_schema_file` methods. Every schema
    will be validated against Jupyter Event's metaschema.
    """

    handlers = Handlers(
        default_value=[],
        allow_none=True,
        help="""A list of logging.Handler instances to send events to.

        When set to None (the default), all events are discarded.
        """,
    ).tag(config=True)

    schemas = Instance(
        SchemaRegistry,
        help="""The SchemaRegistry for caching validated schemas
        and their jsonschema validators.
        """,
    )

    _modifiers = Dict({}, help="A mapping of schemas to their list of modifiers.")

    _modified_listeners = Dict(
        {}, help="A mapping of schemas to the listeners of modified events."
    )

    _unmodified_listeners = Dict(
        {}, help="A mapping of schemas to the listeners of unmodified/raw events."
    )

    _active_listeners = Set()

    async def gather_listeners(self):
        return await asyncio.gather(*self._active_listeners, return_exceptions=True)

    @default("schemas")
    def _default_schemas(self) -> SchemaRegistry:
        return SchemaRegistry()

    def __init__(self, *args, **kwargs):
        # We need to initialize the configurable before
        # adding the logging handlers.
        super().__init__(*args, **kwargs)
        # Use a unique name for the logger so that multiple instances of EventLog do not write
        # to each other's handlers.
        log_name = __name__ + "." + str(id(self))
        self._logger = logging.getLogger(log_name)
        # We don't want events to show up in the default logs
        self._logger.propagate = False
        # We will use log.info to emit
        self._logger.setLevel(logging.INFO)
        # Add each handler to the logger and format the handlers.
        if self.handlers:
            for handler in self.handlers:
                self.register_handler(handler)

    def _load_config(self, cfg, section_names=None, traits=None):
        """Load EventLogger traits from a Config object, patching the
        handlers trait in the Config object to avoid deepcopy errors.
        """
        my_cfg = self._find_my_config(cfg)
        handlers = my_cfg.pop("handlers", [])

        # Turn handlers list into a pickeable function
        def get_handlers():
            return handlers

        my_cfg["handlers"] = get_handlers

        # Build a new eventlog config object.
        eventlogger_cfg = Config({"EventLogger": my_cfg})
        super()._load_config(eventlogger_cfg, section_names=None, traits=None)

    def register_event_schema(self, schema: Union[dict, str, PurePath]):
        """Register this schema with the schema registry.

        Get this registered schema using the EventLogger.schema.get() method.
        """

        event_schema = self.schemas.register(schema)
        key = event_schema.id
        self._modifiers[key] = set()
        self._modified_listeners[key] = set()
        self._unmodified_listeners[key] = set()

    def register_handler(self, handler: logging.Handler):
        """Register a new logging handler to the Event Logger.

        All outgoing messages will be formatted as a JSON string.
        """

        def _skip_message(record, **kwargs):
            """
            Remove 'message' from log record.
            It is always emitted with 'null', and we do not want it,
            since we are always emitting events only
            """
            del record["message"]
            return json.dumps(record, **kwargs)

        formatter = jsonlogger.JsonFormatter(json_serializer=_skip_message)
        handler.setFormatter(formatter)
        self._logger.addHandler(handler)
        if handler not in self.handlers:
            self.handlers.append(handler)

    def remove_handler(self, handler: logging.Handler):
        """Remove a logging handler from the logger and list of handlers."""
        self._logger.removeHandler(handler)
        if handler in self.handlers:
            self.handlers.remove(handler)

    def add_modifier(
        self,
        *,
        schema_id: Union[str, None] = None,
        modifier: Callable[[str, dict], dict],
    ):
        """Add a modifier (callable) to a registered event.

        Parameters
        ----------
        modifier: Callable
            A callable function/method that executes when the named event occurs.
            This method enforces a string signature for modifiers:

                (schema_id: str, data: dict) -> dict:
        """
        # Ensure that this is a callable function/method
        if not callable(modifier):
            raise TypeError("`modifier` must be a callable")

        # Now let's verify the function signature.
        signature = inspect.signature(modifier)

        def modifier_signature(schema_id: str, data: dict) -> dict:
            """Signature to enforce"""
            ...

        expected_signature = inspect.signature(modifier_signature)
        # Assert this signature or raise an exception
        if signature == expected_signature:
            # If the schema ID and version is given, only add
            # this modifier to that schema
            if schema_id:
                self._modifiers[schema_id].add(modifier)
                return
            for id in self._modifiers:
                if schema_id is None or id == schema_id:
                    self._modifiers[id].add(modifier)
        else:
            raise ModifierError(
                "Modifiers are required to follow an exact function/method "
                "signature. The signature should look like:"
                f"\n\n\tdef my_modifier{expected_signature}:\n\n"
                "Check that you are using type annotations for each argument "
                "and the return value."
            )

    def remove_modifier(
        self, *, schema_id: str = None, modifier: Callable[[str, dict], dict]
    ) -> None:
        """Remove a modifier from an event or all events.

        Parameters
        ----------
        schema_id: str
            If given, remove this modifier only for a specific event type.
        modifier: Callable[[str, dict], dict]

            The modifier to remove.
        """
        # If schema_id is given remove the modifier from this schema.
        if schema_id:
            self._modifiers[schema_id].discard(modifier)
        # If no schema_id is given, remove the modifier from all events.
        else:
            for schema_id in self.schemas.schema_ids:
                # Remove the modifier if it is found in the list.
                self._modifiers[schema_id].discard(modifier)
                self._modifiers[schema_id].discard(modifier)

    def add_listener(
        self,
        *,
        modified: bool = True,
        schema_id: Union[str, None] = None,
        listener: Callable[["EventLogger", str, dict], None],
    ):
        """Add a listener (callable) to a registered event.

        Parameters
        ----------
        modified: bool
            If True (default), listens to the data after it has been mutated/modified
            by the list of modifiers.
        schema_id: str
            $id of the schema
        listener: Callable
            A callable function/method that executes when the named event occurs.
        """
        if not callable(listener):
            raise TypeError("`listener` must be a callable")

        signature = inspect.signature(listener)

        async def listener_signature(
            logger: EventLogger, schema_id: str, data: dict
        ) -> None:
            ...

        expected_signature = inspect.signature(listener_signature)
        # Assert this signature or raise an exception
        if signature == expected_signature:
            # If the schema ID and version is given, only add
            # this modifier to that schema
            if schema_id:
                if modified:
                    self._modified_listeners[schema_id].add(listener)
                    return
                self._unmodified_listeners[schema_id].add(listener)
            for id in self.schemas.schema_ids:
                if schema_id is None or id == schema_id:
                    if modified:
                        self._modified_listeners[id].add(listener)
                    else:
                        self._unmodified_listeners[schema_id].add(listener)
        else:
            raise ListenerError(
                "Listeners are required to follow an exact function/method "
                "signature. The signature should look like:"
                f"\n\n\tasync def my_listener{expected_signature}:\n\n"
                "Check that you are using type annotations for each argument "
                "and the return value."
            )

    def remove_listener(
        self,
        *,
        schema_id: str = None,
        listener: Callable[["EventLogger", str, dict], None],
    ) -> None:
        """Remove a listener from an event or all events.

        Parameters
        ----------
        schema_id: str
            If given, remove this modifier only for a specific event type.

        listener: Callable[[EventLogger, str, dict], dict]
            The modifier to remove.
        """
        # If schema_id is given remove the listener from this schema.
        if schema_id:
            self._modified_listeners[schema_id].discard(listener)
            self._unmodified_listeners[schema_id].discard(listener)
        # If no schema_id is given, remove the listener from all events.
        else:
            for schema_id in self.schemas.schema_ids:
                # Remove the listener if it is found in the list.
                self._modified_listeners[schema_id].discard(listener)
                self._unmodified_listeners[schema_id].discard(listener)

    def emit(self, *, schema_id: str, data: dict, timestamp_override=None):
        """
        Record given event with schema has occurred.

        Parameters
        ----------
        schema_id: str
            $id of the schema
        data: dict
            The event to record
        timestamp_override: datetime, optional
            Optionally override the event timestamp. By default it is set to the current timestamp.

        Returns
        -------
        dict
            The recorded event data
        """
        # If no handlers are routing these events, there's no need to proceed.
        if (
            not self.handlers
            and not self._modified_listeners[schema_id]
            and not self._unmodified_listeners[schema_id]
        ):
            return

        # If the schema hasn't been registered, raise a warning to make sure
        # this was intended.
        if schema_id not in self.schemas:
            warnings.warn(
                f"{schema_id} has not been registered yet. If "
                "this was not intentional, please register the schema using the "
                "`register_event_schema` method.",
                SchemaNotRegistered,
            )
            return

        schema = self.schemas.get(schema_id)

        # Deep copy the data and modify the copy.
        modified_data = copy.deepcopy(data)
        for modifier in self._modifiers[schema.id]:
            modified_data = modifier(schema_id=schema_id, data=modified_data)

        if self._unmodified_listeners[schema.id]:
            # Process this event, i.e. validate and modify (in place)
            self.schemas.validate_event(schema_id, data)

        # Validate the modified data.
        self.schemas.validate_event(schema_id, modified_data)

        # Generate the empty event capsule.
        if timestamp_override is None:
            timestamp = datetime.utcnow()
        else:
            timestamp = timestamp_override
        capsule = {
            "__timestamp__": timestamp.isoformat() + "Z",
            "__schema__": schema_id,
            "__schema_version__": schema.version,
            "__metadata_version__": EVENTS_METADATA_VERSION,
        }
        capsule.update(modified_data)

        self._logger.info(capsule)

        # callback for removing from finished listeners
        # from active listeners set.
        def _listener_task_done(task: asyncio.Task):
            # If an exception happens, log it to the main
            # applications logger
            err = task.exception()
            if err:
                self.log.error(err)
            self._active_listeners.discard(task)

        # Loop over listeners and execute them.
        for listener in self._modified_listeners[schema_id]:
            # Schedule this listener as a task and add
            # it to the list of active listeners
            task = asyncio.create_task(
                listener(
                    logger=self,
                    schema_id=schema_id,
                    data=modified_data,
                )
            )
            self._active_listeners.add(task)

            # Adds the task and cleans it up later if needed.
            task.add_done_callback(_listener_task_done)

        for listener in self._unmodified_listeners[schema_id]:
            task = asyncio.create_task(
                listener(logger=self, schema_id=schema_id, data=data)
            )
            self._active_listeners.add(task)

            # Remove task from active listeners once its finished.
            def _listener_task_done(task: asyncio.Task):
                # If an exception happens, log it to the main
                # applications logger
                err = task.exception()
                if err:
                    self.log.error(err)
                self._active_listeners.discard(task)

            # Adds the task and cleans it up later if needed.
            task.add_done_callback(_listener_task_done)

        return capsule
