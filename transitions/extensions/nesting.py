from ..core import Machine, Transition, State, Event, listify, MachineError, EventData

from six import string_types
from os.path import commonprefix
import copy

import logging
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class FunctionWrapper(object):
    def __init__(self, func, path):
        if len(path) > 0:
            self.add(func, path)
            self._func = None
        else:
            self._func = func

    def add(self, func, path):
        if len(path) > 0:
            name = path[0]
            if name[0].isdigit():
                name = '_' + name
            if hasattr(self, name):
                getattr(self, name).add(func, path[1:])
            else:
                x = FunctionWrapper(func, path[1:])
                setattr(self, name, x)
        else:
            self._func = func

    def __call__(self, *args, **kwargs):
        return self._func(*args, **kwargs)


# Added parent and children parameter children is a list of NestedStates
# and parent is the full name of the parent e.g. Foo_Bar_Baz.
class NestedState(State):
    separator = '.'

    def __init__(self, name, on_enter=None, on_exit=None, ignore_invalid_triggers=None, children=None, parent=None):
        self._name = name
        self.parent = parent
        super(NestedState, self).__init__(name=name, on_enter=on_enter, on_exit=on_exit,
                                          ignore_invalid_triggers=ignore_invalid_triggers)
        self.children = children

    @property
    def level(self):
        return self.parent.level + 1 if self.parent is not None else 0

    @property
    def name(self):
        return (self.parent.name + NestedState.separator + self._name) if self.parent else self._name

    @name.setter
    def name(self, value):
        self._name = value

    def exit_nested(self, event_data, target_state=None):
        if target_state and target_state.level > 0 and self.level > 0:
            if self.level > target_state.level:
                self.exit(event_data)
                return self.parent.exit_nested(event_data, target_state)
            elif self.level <= target_state.level:
                tmp_state = target_state
                while self.level != tmp_state.level:
                    tmp_state = target_state.parent
                tmp_self = self
                while tmp_self.level > 0 and tmp_state.parent.name != tmp_self.parent.name:
                    tmp_self.exit(event_data)
                    tmp_self = tmp_self.parent
                    tmp_state = tmp_state.parent
                tmp_self.exit(event_data)
                return tmp_self.level
        else:
            self.exit(event_data)
            if self.parent:
                return self.parent.exit_nested(event_data, None)
        return 0

    def enter_nested(self, event_data, level=None):
        if level is not None and level != self.level:
            self.parent.enter_nested(event_data, level)
        self.enter(event_data)


class NestedTransition(Transition):
    # The actual state change method 'execute' in Transition was restructured to allow overriding
    def _change_state(self, event_data):
        machine = event_data.machine
        dest_state = machine.get_state(self.dest)
        source_state = machine.current_state
        lvl = source_state.exit_nested(event_data, dest_state)
        event_data.machine.set_state(self.dest)
        event_data.update()
        dest_state.enter_nested(event_data, lvl)


class NestedEvent(Event):

    def trigger(self, *args, **kwargs):
        tmp = self.machine.current_state
        while tmp.parent and tmp.name not in self.transitions:
            tmp = tmp.parent
        if tmp.name not in self.transitions:
            msg = "Can't trigger event %s from state %s!" % (self.name,
                                                             self.machine.current_state.name)
            if self.machine.current_state.ignore_invalid_triggers:
                logger.warning(msg)
            else:
                raise MachineError(msg)
        event = EventData(self.machine.current_state, self, self.machine,
                          self.machine.model, args=args, kwargs=kwargs)
        for t in self.transitions[tmp.name]:
            event.transition = t
            if t.execute(event):
                return True
        return False


class HierarchicalMachine(Machine):

    def __init__(self, *args, **kwargs):
        self._buffered_transitions = []
        super(HierarchicalMachine, self).__init__(*args, **kwargs)

    # Instead of creating transitions directly, Machine now use a factory method which can be overridden
    @staticmethod
    def _create_transition(*args, **kwargs):
        return NestedTransition(*args, **kwargs)


    # TODO rework to_blueprint
    # TODO solve Event Locked/Nested issue
    # The chosen approach for hierarchical state machines was 'flatten' which means that nested states
    # are converted into linked states with a naming scheme that concatenates the state name with
    # its parent's name. Substate Bar of Foo becomes Foo_Bar. An alternative approach would be to use actual nested
    # state machines.
    def traverse(self, states, on_enter=None, on_exit=None,
                 ignore_invalid_triggers=None, parent=None, remap={}):
        states = listify(states)
        new_states = []
        ignore = ignore_invalid_triggers
        if ignore is None:
            ignore = self.ignore_invalid_triggers
        for state in states:
            tmp_states = []
            # other state representations are handled almost like in the base class but a parent parameter is added
            if isinstance(state, string_types):
                if state in remap:
                    continue
                tmp_states.append(NestedState(state, on_enter=on_enter, on_exit=on_exit, parent=parent,
                                              ignore_invalid_triggers=ignore))
            elif isinstance(state, dict):
                state = copy.deepcopy(state)
                if 'ignore_invalid_triggers' not in state:
                    state['ignore_invalid_triggers'] = ignore
                state['parent'] = parent

                if 'children' in state:
                    # Concat the state names with the current scope. The scope is the concatenation of all
                    # previous parents. Call _flatten again to check for more nested states.
                    p = NestedState(state['name'], on_enter=on_enter, on_exit=on_exit,
                                    ignore_invalid_triggers=ignore, parent=parent)
                    p.children = self.traverse(state['children'], on_enter=on_enter, on_exit=on_exit,
                                               ignore_invalid_triggers=ignore,
                                               parent=p, remap=state.get('remap', {}))
                    tmp_states.append(p)
                    tmp_states.extend(p.children)
                else:
                    tmp_states.insert(0, NestedState(**state))
            elif isinstance(state, HierarchicalMachine):
                new_states = [s for s in state.states.values() if s.level == 0 and s.name not in remap]
                for s in new_states:
                    s.parent = parent
                tmp_states.extend(new_states)
                for trigger, event in state.events.items():
                    if trigger.startswith('to_'):
                        path = trigger[3:].split(NestedState.separator)
                        ppath = parent.name.split(NestedState.separator)
                        path = ['to_'+ppath[0]] + ppath[1:] + path
                        trigger = '.'.join(path)
                    for transitions in event.transitions.values():
                        for transition in transitions:
                            src = transition.source
                            dst = parent.name + NestedState.separator + transition.dest\
                                if transition.dest not in remap else remap[transition.dest]
                            self._buffered_transitions.append({'trigger': trigger,
                                                               'source': parent.name + NestedState.separator + src,
                                                               'dest': dst,
                                                               'conditions': transition.conditions,
                                                               'before': transition.before,
                                                               'after': transition.after})

            elif isinstance(state, NestedState):
                tmp_states.append(state)

            new_states.extend(tmp_states)
        return new_states

    def add_states(self, states, *args, **kwargs):
        # preprocess states to flatten the configuration and resolve nesting
        new_states = self.traverse(states, *args, **kwargs)
        super(HierarchicalMachine, self).add_states(new_states, *args, **kwargs)

        # for t in self._buffered_transitions:
        #     print(t['trigger'])
        while len(self._buffered_transitions) > 0:
            args = self._buffered_transitions.pop()
            self.add_transition(**args)

    def add_transition(self, trigger, source, dest, conditions=None,
                       unless=None, before=None, after=None):
        if isinstance(source, string_types):
            source = [x.name for x in self.states.values()] if source == '*' else [source]

        if trigger not in self.events:
            self.events[trigger] = NestedEvent(trigger, self)
            if trigger.startswith('to_'):
                path = trigger[3:].split(NestedState.separator)
                if hasattr(self.model, 'to_' + path[0]):
                    t = getattr(self.model, 'to_' + path[0])
                    t.add(self.events[trigger].trigger, path[1:])
                else:
                    t = FunctionWrapper(self.events[trigger].trigger, path[1:])
                    setattr(self.model, 'to_' + path[0], t)
            else:
                setattr(self.model, trigger, self.events[trigger].trigger)
        super(HierarchicalMachine, self).add_transition(trigger, source, dest, conditions=conditions,
                                                        unless=unless, before=before, after=after)

    def on_enter(self, state_name, callback):
        self.get_state(state_name).add_callback('enter', callback)

    def on_exit(self, state_name, callback):
        self.get_state(state_name).add_callback('exit', callback)