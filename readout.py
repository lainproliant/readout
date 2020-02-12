# --------------------------------------------------------------------
# readout - A framework for detecting changes and reacting to them.
#
# Author: Lain Musgrove (lain.proliant@gmail.com)
# Date: Saturday January 18, 2020
#
# Distributed under terms of the MIT license.
# --------------------------------------------------------------------

import asyncio
import inspect
import logging
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import (
    Any,
    Awaitable,
    Collection,
    Dict,
    Generic,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
)

import ansilog
import nanoid
from lexex import Lexeme, Lexer

# --------------------------------------------------------------------
if os.environ.get("READOUT_DEBUG") == "1":
    ansilog.handler.setLevel(logging.DEBUG)

# --------------------------------------------------------------------
T = TypeVar("T")
U = TypeVar("U")

# --------------------------------------------------------------------
NAME_RX = "[a-zA-Z_][a-zA-Z_0-9]*"
ANY_STATE = "@ANY@"

# --------------------------------------------------------------------
predicate_lex = Lexer(
    {
        Lexer.ROOT: ((Lexer.IGNORE, "\\s+"), ("name", NAME_RX, "name")),
        "name": (
            (Lexer.IGNORE, "\\s+"),
            ("at", "@", "state"),
            ("op", "<=", "expr", lambda a, b: a <= b),
            ("op", ">=", "expr", lambda a, b: a >= b),
            ("op", "!=", "expr", lambda a, b: a != b),
            ("op", "<", "expr", lambda a, b: a < b),
            ("op", ">", "expr", lambda a, b: a > b),
            ("op", "=", "expr", lambda a, b: a == b),
        ),
        "expr": (
            (Lexer.IGNORE, "\\s+"),
            ("value", "[0-9]*\\.[0-9]+", "end", lambda x: float(x)),
            ("value", "[0-9]+", "end", lambda x: int(x)),
            ("value", NAME_RX, "end", lambda x: str(x)),
        ),
        "state": (
            (Lexer.IGNORE, "\\s+"),
            ("state", NAME_RX, "state_to_qq", lambda x: str(x)),
        ),
        "state_to_qq": ((Lexer.IGNORE, "\\s+"), ("to", "->", "state_to"),),
        "state_to": (
            (Lexer.IGNORE, "\\s+"),
            ("state", NAME_RX, "state_to", lambda x: str(x)),
        ),
        "end": ((Lexer.IGNORE, "\\s+"),),
    }
)


# --------------------------------------------------------------------
def get_id():
    """Get a random small string ID."""
    return nanoid.generate(size=10)


# --------------------------------------------------------------------
async def async_map(coro: Awaitable[T], value: U) -> Tuple[T, U]:
    """Pair another value to be returned with an awaitable."""
    return await coro, value


# --------------------------------------------------------------------
async def sh(cmd: str, timeout=timedelta(seconds=10), stdout=True, stderr=False):
    """Invoke a shell command, capturing the output and returning it.
    If `stdout` or `stderr` are set to True (stdin by default is True), the output
    is decoded and returned.  If both are True, a tuple of both decoded outputs
    is returned.  If none are True, None is returned."""

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout.total_seconds())

    if stdout and stderr:
        return out.decode("utf-8").strip(), err.decode("utf-8").strip()
    if stdout:
        return out.decode("utf-8").strip()
    if stderr:
        return err.decode("utf-8").strip()
    return None


# --------------------------------------------------------------------
class ScheduledItem:
    """An object that is updated/invoked at a given interval."""

    def __init__(self, freq: timedelta):
        self.freq = freq
        self.last_updated_time = datetime(1900, 1, 1)

    def scheduled_for(self) -> datetime:
        """Get the next scheduled time this item should be updated/invoked."""
        return self.last_updated_time + self.freq

    def schedule_next(self):
        self.last_updated_time = datetime.now()

    async def update(self) -> bool:
        raise NotImplementedError()


# --------------------------------------------------------------------
@dataclass
class Readout(ScheduledItem, Generic[T]):
    """A generic class for an object that provides quantitative (Sensor) or
    qualitative (Gauge) readings at regular intervals."""

    log = ansilog.getLogger("sensors.Readout")

    def __init__(self, name: str, freq: timedelta):
        super().__init__(freq)
        self.value: Optional[T] = None
        self.name = name
        self.freq = freq
        self.id = get_id()

    def scheduled_for(self) -> datetime:
        """Get the next scheduled time this readout should be read."""
        return self.last_updated_time + self.freq

    async def get_value(self) -> T:
        """Update the value of the readout from some outside source."""
        raise NotImplementedError()

    def read(self) -> T:
        """Read the latest value from the readout.  Raises ValueError if the
        readout has never been updated."""
        if self.value is not None:
            return self.value
        raise ValueError("The sensor has never been updated.")

    async def update(self) -> bool:
        """Trigger the readout value to be updated, then schedules the next
        update time.  Returns True if the readout value changed, False
        otherwise."""
        self.schedule_next()
        old_value = self.value
        try:
            self.value = await self.get_value()
            if self.value != old_value:
                self.log.debug(
                    f"Readout changed: '{self.name}' ({old_value} -> {self.value})"
                )
            return self.value != old_value
        except asyncio.TimeoutError:
            self.log.error(f"Timed out waiting for readout '{self.name}' to update.")
            return False
        except Exception:
            self.log.exception(
                f"Failed to update readout '{self.name}' due to an exception."
            )
            return False

    def __repr__(self):
        return f"Readout<name={self.name}, id={str(self.id)}"


# --------------------------------------------------------------------
Sensor = Readout[str]
Gauge = Readout[int]


# --------------------------------------------------------------------
class Predicate:
    """An abstract class for an object which encloses the logical predicate of
    an event.  In other words, this object is responsible for checking if a
    given condition is True, which if True, an event should occur."""

    def __init__(self, relevance: Set[str] = set()):
        self.id = get_id()
        self.relevance = relevance

    async def check(self, engine: "Engine"):
        raise NotImplementedError()

    def relevant_to_readout(self, readout: Readout) -> bool:
        return readout.name in self.relevance

    def relevant_to_state_machine(self, machine: "StateMachine") -> bool:
        return f"{machine.name}@" in self.relevance

    def signature(self) -> str:
        """Return a unique signature for this predicate.  This could be an ID
        or a string representation of the logical predicate itself, but must
        be unique from different predicates."""
        raise NotImplementedError()

    @staticmethod
    def parse(expr: str) -> "Predicate":
        """Parse a string expression corresponding to an ExpressionPredicate or
        StateTransitionPredicate.  Returns the parsed predicate.  Raises
        ValueError if the string could not be parsed."""
        tokens = predicate_lex.tokenize(expr)
        if len(tokens) == 3 and tokens[1].type.name == "op":
            return ExpressionPredicate(*tokens)
        if len(tokens) >= 3 and tokens[1].type.name == "at":
            return StateTransitionPredicate(*tokens)
        raise ValueError(f"Could not parse predicate expression: '{expr}'")


# --------------------------------------------------------------------
class ExpressionPredicate(Predicate):
    """A predicate described by a small DSL, where named readouts can be
    compared with string or integer values.  For a definition of this DSL, see
    `readout.predicate_lex`."""

    def __init__(self, readout_t: Lexeme, op_t: Lexeme, value_t: Lexeme):
        self.readout_name = readout_t.content
        self.op = op_t.type.data
        self.value = value_t.type.data(value_t.content)
        self._signature = readout_t.content + op_t.content + value_t.content
        super().__init__(set([self.readout_name]))

    async def check(self, engine: "Engine"):
        return self.op(engine.get_readout(self.readout_name).read(), self.value)

    def signature(self):
        return self._signature


# --------------------------------------------------------------------
class StateTransitionPredicate(Predicate):
    """A predicate described by a small DSL, where entering a given state,
    exiting a given state, or entering a given state from another given state
    can be specified.  For a definition of this DSL, see
    `readout.predicate_lex`."""

    def __init__(
        self,
        name_t: Lexeme,
        at_t: Lexeme,
        state_t: Lexeme,
        to_t: Lexeme = None,
        to_state_t: Lexeme = None,
    ):
        self.machine_name = name_t.content
        self.state = state_t.content
        self._signature = name_t.content + at_t.content + state_t.content
        if to_t is not None:
            self.to_state = to_state_t.content if to_state_t else ANY_STATE
            self._signature += to_t.content
            if to_state_t is not None:
                self._signature += to_state_t.content
        else:
            self.to_state = None
        super().__init__(set([f"{self.machine_name}@"]))

    async def check(self, engine: "Engine"):
        machine = engine.get_state_machine(self.machine_name)
        if self.to_state is ANY_STATE:
            return machine.from_state == self.state
        if self.to_state is not None:
            return machine.from_state == self.state and machine.state == self.to_state
        return machine.state == self.state

    def signature(self):
        return self._signature


# --------------------------------------------------------------------
class CompoundPredicate(Predicate):
    """A combination of multiple predicates, all of which must be True to satisfy."""

    def __init__(self, predicates: Sequence[Predicate]):
        self.predicates = predicates
        super().__init__(self._flat_relevance())

    def _flat_relevance(self):
        relevance: Set[str] = set()
        for predicate in self.predicates:
            if isinstance(predicate, CompoundPredicate):
                relevance.update(predicate._flat_relevance())
            else:
                relevance.update(predicate.relevance)
        return relevance

    async def check(self, engine: "Engine"):
        results = await asyncio.gather(*(p.check(engine) for p in self.predicates))
        return all(results)

    def signature(self):
        return ",".join(sorted(p.signature() for p in self.predicates))


# --------------------------------------------------------------------
@dataclass
class Event:
    name: str
    predicate_id: str
    id: str = field(default_factory=get_id)


# --------------------------------------------------------------------
@dataclass
class StateMachine:
    name: str
    state: str = "init"
    from_state: Optional[str] = None


# --------------------------------------------------------------------
class EventHandler:
    def __init__(self, event_id):
        self.event_id = event_id

    async def handle(self, event: Event, engine: "Engine") -> None:
        raise NotImplementedError()


# --------------------------------------------------------------------
class Agent(ScheduledItem):
    """An autonomous agent which is invoked at a given interval."""

    def __init__(self, engine: "Engine", freq: timedelta):
        super().__init__(freq)
        self.engine = engine

    async def invoke(self) -> None:
        raise NotImplementedError()

    async def update(self) -> bool:
        self.schedule_next()
        await self.invoke()
        return True

    def signature(self) -> str:
        """Return a unique signature for this agent."""
        raise NotImplementedError()


# --------------------------------------------------------------------
class Engine:
    def __init__(self, name="Sensor Engine"):
        self.names = set()
        self.readouts_by_name: Dict[str, str] = {}
        self.readouts_table: Dict[str, Readout[Any]] = {}
        self.predicates_table: Dict[str, Predicate] = {}
        self.events_table: Dict[str, Event] = {}
        self.events_by_name: Dict[str, str] = {}
        self.events_by_predicate: Dict[str, List[str]] = {}
        self.handlers_table: Dict[str, List[EventHandler]] = {}
        self.predicates_by_signature: Dict[str, str] = {}
        self.machine_table: Dict[str, StateMachine] = {}
        self.agents: List[Agent] = []
        self.name = name
        self.log = ansilog.getLogger("sensors.Engine")
        self.log.setLevel(logging.DEBUG)
        self._alive = True

    def shutdown(self):
        self.log.debug(f"Shutdown requested for '{self.name}'.")
        self._alive = False

    def add_agent(self, agent: Agent) -> Agent:
        self.agents.append(agent)
        self.log.debug(f"Added agent: '{agent.signature()}'")
        return agent

    def add_predicate(self, predicate: Predicate) -> Predicate:
        signature = predicate.signature()
        if signature in self.predicates_by_signature:
            return self.get_predicate(signature=signature)
        self.predicates_table[predicate.id] = predicate
        self.predicates_by_signature[signature] = predicate.id
        self.log.debug(f"Added predicate: '{signature}' ({predicate.id}).")
        return predicate

    def get_predicate(
        self, id: Optional[str] = None, signature: Optional[str] = None
    ) -> Predicate:
        assert id or signature
        if signature and signature in self.predicates_by_signature:
            return self.get_predicate(id=self.predicates_by_signature[signature])
        if not id or id not in self.predicates_table:
            raise KeyError(f"Predicate not found for {id=} or {signature=}.")
        return self.predicates_table[id]

    def add_event(self, event: Event) -> Event:
        assert event.name not in self.names
        predicate = self.get_predicate(id=event.predicate_id)
        self.events_table[event.id] = event
        self.events_by_name[event.name] = event.id
        self.names.add(event.name)
        events_by_predicate = self.events_by_predicate.get(event.predicate_id, [])
        events_by_predicate.append(event.id)
        self.events_by_predicate[event.predicate_id] = events_by_predicate
        self.log.debug(
            f"Added event: '{event.name}' ({event.id}) with predicate '{predicate.signature()}' ({predicate.id})"
        )
        return event

    def get_events_for_predicate(self, predicate: Predicate):
        event_ids = self.events_by_predicate.get(predicate.id, [])
        return [self.get_event(id=id) for id in event_ids]

    def get_event(self, name: str = None, id: str = None) -> Event:
        assert name or id
        if name:
            id = self.events_by_name.get(name, None)
        if not id or id not in self.events_table:
            raise KeyError(f"Event not found for {name=} or {id=}.")
        return self.events_table[id]

    async def trigger_event(self, event: Event):
        self.log.debug(f"Triggered event: '{event.name}' ({event.id})")
        await asyncio.gather(
            *(h.handle(event, self) for h in self.get_handlers_for_event(event))
        )

    def add_handler(self, handler: EventHandler) -> EventHandler:
        event = self.get_event(id=handler.event_id)
        handlers = self.handlers_table.get(handler.event_id, [])
        handlers.append(handler)
        self.handlers_table[handler.event_id] = handlers
        self.log.debug(f"Added event handler for '{event.name}' ({event.id}).")
        return handler

    def get_handlers_for_event(self, event: Event) -> Sequence[EventHandler]:
        return [*self.handlers_table.get(event.id, [])]

    def add_readout(self, readout: Readout[T]) -> Readout[T]:
        assert readout.name not in self.names
        self.readouts_table[readout.id] = readout
        self.readouts_by_name[readout.name] = readout.id
        self.names.add(readout.name)
        self.log.debug(f"Added readout: '{readout.name}'")
        return readout

    def get_readout(self, name: str = None, id: str = None) -> Readout[T]:
        if name:
            id = self.readouts_by_name.get(name, None)
        if not id or id not in self.readouts_table:
            raise KeyError(f"Readout not found for {name=} or {id=}.")
        return self.readouts_table[id]

    def add_state_machine(self, machine: StateMachine) -> StateMachine:
        assert machine.name not in self.names
        if machine.name in self.machine_table:
            return self.machine_table[machine.name]
        self.machine_table[machine.name] = machine
        self.names.add(machine.name)
        return machine

    def get_state_machine(self, name: str) -> StateMachine:
        if name not in self.machine_table:
            raise ValueError(f"Machine not found for {name=}.")
        return self.machine_table[name]

    async def set_machine_state(self, machine_name: str, state: str):
        machine = self.get_state_machine(machine_name)
        if machine.state == state:
            return
        machine.from_state = machine.state
        machine.state = state
        self.log.info(
            f"{ansilog.fg.cyan('[' + machine.name + ']')} {machine.from_state} -> {machine.state}."
        )
        try:
            predicates = await self._check_predicates_for_state_machine(machine)
            events = self._get_events_for_predicates(predicates)
            await self._trigger_events(events)

        finally:
            machine.from_state = None

    def _mk_readout_decorator(self, base_class: Type, freq):
        def decorator(f):
            class FuncReadout(base_class):  # type: ignore
                async def get_value(self):
                    if inspect.iscoroutinefunction(f):
                        return await f()
                    result = f()
                    if inspect.iscoroutine(result):
                        return await result
                    return result

            if isinstance(freq, timedelta):
                frequency = freq
            else:
                frequency = timedelta(seconds=freq)

            readout = FuncReadout(f.__name__, frequency)
            self.add_readout(readout)
            return readout

        return decorator

    def sensor(self, freq=timedelta(seconds=10)):
        return self._mk_readout_decorator(Sensor, freq)

    def gauge(self, freq=timedelta(seconds=10)):
        return self._mk_readout_decorator(Gauge, freq)

    def when(self, *conditions, event_name=None):
        predicate: Predicate
        sub_predicates: List[Predicate] = []

        for condition in conditions:
            if isinstance(condition, Predicate):
                sub_predicates.append(condition)
            elif isinstance(condition, str):
                sub_predicates.append(Predicate.parse(condition))
            else:
                raise ValueError(f"Unsupported condition type: '{type(condition)}'.")

        if len(sub_predicates) > 1:
            predicate = CompoundPredicate(sub_predicates)
        else:
            predicate = sub_predicates[0]

        predicate = self.add_predicate(predicate)

        def decorator(f):
            name = event_name if f.__name__ == "__handler" else f.__name__
            if name is None:
                name = f"handler-{get_id()}"
            event = self.add_event(Event(name, predicate.id))

            class FuncEventHandler(EventHandler):
                def __init__(self, event):
                    super().__init__(event.id)

                async def handle(self, event, engine) -> None:
                    kwargs: Dict[str, Any] = {}
                    params = inspect.signature(f).parameters
                    if "engine" in params:
                        kwargs["engine"] = engine
                    if "event" in params:
                        kwargs["event"] = event
                    if inspect.iscoroutinefunction(f):
                        await f(**kwargs)
                    else:
                        f(**kwargs)

            handler = FuncEventHandler(event)
            self.add_handler(handler)
            return handler

        return decorator

    def state(self, expr):
        predicate = Predicate.parse(expr)
        if not isinstance(predicate, StateTransitionPredicate):
            raise ValueError(f"Invalid state expression: '{expr}'.")
        try:
            machine = self.get_state_machine(predicate.machine_name)
        except ValueError:
            machine = self.add_state_machine(StateMachine(predicate.machine_name))
        state = predicate.to_state or predicate.state

        def decorator(f):
            async def __handler(*args, **kwargs):
                await self.set_machine_state(machine.name, state)

            self.when(expr, event_name=get_id())(f)

            return __handler

        return decorator

    def agent(self, freq=timedelta(seconds=10)):
        def decorator(f):
            class FuncAgent(Agent):
                def __init__(self, engine: "Engine", freq: timedelta):
                    super().__init__(engine, freq)

                async def invoke(self) -> None:
                    params = inspect.signature(f).parameters.keys()
                    kwargs = self.engine._get_agent_params(params)
                    if inspect.iscoroutinefunction(f):
                        await f(**kwargs)
                    else:
                        f(**kwargs)

                def signature(self) -> str:
                    return f.__qualname__

            agent = FuncAgent(self, freq)
            self.add_agent(agent)
            return agent

        return decorator

    def _on_sigint(self, signal, frame):
        self.log.info("")
        self.log.warning("Received SIGINT, shutting down...")
        self.shutdown()

    def _on_sigterm(self, signal, frame):
        self.log.info("")
        self.log.warning("Received SIGTERM, shutting down...")
        self.shutdown()

    def start(self):
        old_sigterm_handler = signal.signal(signal.SIGTERM, self._on_sigterm)
        old_sigint_handler = signal.signal(signal.SIGINT, self._on_sigint)
        try:
            loop = asyncio.get_event_loop()
            if self._alive:
                self.log.info(f"{ansilog.fg.green('[start]')} {self.name}.")
            else:
                raise RuntimeError(
                    f"'{self.name}' has already shutdown and can't be started again."
                )

            while self._alive:
                loop.run_until_complete(self.run(loop))

            self.log.info(f"{ansilog.fg.red('[stop]')} {self.name}.")
        finally:
            signal.signal(signal.SIGTERM, old_sigterm_handler)
            signal.signal(signal.SIGINT, old_sigint_handler)

    async def run(self, loop: asyncio.AbstractEventLoop):
        updated_readouts = await self._update_readouts()
        satisfied_predicates = await self._check_predicates_for_readouts(
            updated_readouts
        )
        events = self._get_events_for_predicates(satisfied_predicates)
        await self._trigger_events(events)
        await self._update_agents()

        item = self._get_next_scheduled_item()
        if not item:
            self.log.error("There are no items to update.  Bailing out.")
            self.shutdown()
            return

        now = datetime.now()
        next_update = item.scheduled_for()
        if next_update > now:
            await asyncio.sleep((next_update - now).total_seconds())

    def _get_readouts_pending_update(self) -> Sequence[Readout]:
        return [
            r
            for r in self.readouts_table.values()
            if r.scheduled_for() <= datetime.now()
        ]

    async def _update_readouts(self) -> Sequence[Readout]:
        pending_readouts = self._get_readouts_pending_update()
        updated_readouts: List[Readout] = []
        for result in await asyncio.gather(
            *(async_map(r.update(), r) for r in pending_readouts)
        ):
            if result is None:
                continue
            updated, readout = result
            if updated:
                updated_readouts.append(readout)
        return updated_readouts

    async def _update_agents(self) -> None:
        await asyncio.gather(*(a.update() for a in self.agents))

    async def _check_predicates(
        self, predicates: Sequence[Predicate]
    ) -> Sequence[Predicate]:
        satisfied_predicates: List[Predicate] = []
        for result in await asyncio.gather(
            *(async_map(p.check(self), p) for p in predicates)
        ):
            if result is None:
                continue
            satisfied, predicate = result
            if satisfied:
                satisfied_predicates.append(predicate)
        return satisfied_predicates

    async def _check_predicates_for_readouts(
        self, readouts: Sequence[Readout]
    ) -> Sequence[Predicate]:
        predicates_to_check: List[Predicate] = []
        for predicate in self.predicates_table.values():
            if any(predicate.relevant_to_readout(r) for r in readouts):
                predicates_to_check.append(predicate)
        return await self._check_predicates(predicates_to_check)

    async def _check_predicates_for_state_machine(
        self, machine: StateMachine
    ) -> Sequence[Predicate]:
        return await self._check_predicates(
            [
                p
                for p in self.predicates_table.values()
                if p.relevant_to_state_machine(machine)
            ]
        )

    def _get_events_for_predicates(
        self, predicates: Sequence[Predicate]
    ) -> Sequence[Event]:
        events: List[Event] = []
        for predicate in predicates:
            events.extend(self.get_events_for_predicate(predicate))
        return events

    async def _trigger_events(self, events: Sequence[Event]):
        await asyncio.gather(*(self.trigger_event(e) for e in events))

    def _get_next_scheduled_item(self) -> Optional[ScheduledItem]:
        if not self.readouts_table and not self.agents:
            return None
        return min(
            [*self.readouts_table.values(), *self.agents],
            key=lambda r: r.scheduled_for(),
        )

    def _get_agent_param(self, param: str) -> Any:
        try:
            machine = self.get_state_machine(param)
            return machine.state
        except ValueError:
            pass

        try:
            readout: Readout[Any] = self.get_readout(param)
            return readout.read()
        except ValueError:
            return None

    def _get_agent_params(self, params: Collection[str]) -> Dict[str, Any]:
        return {p: self._get_agent_param(p) for p in params}


# --------------------------------------------------------------------
engine = Engine()
agent = engine.agent
sensor = engine.sensor
gauge = engine.gauge
when = engine.when
state = engine.state
start = engine.start
