# SPDX-FileCopyrightText: Copyright (c) 2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A simplified modeling of the CoFlows engine."""
import copy
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from functools import cmp_to_key
from typing import Deque, Dict, List, Optional

from nemoguardrails.colang.v1_1.runtime.events import (
    create_abort_flow_internal_event,
    create_flow_failed_internal_event,
    create_flow_finished_internal_event,
    create_flow_started_internal_event,
)
from nemoguardrails.colang.v1_1.runtime.sliding import slide
from nemoguardrails.colang.v1_1.runtime.utils import create_readable_uuid
from nemoguardrails.utils import new_event_dict

# from rich.logging import RichHandler  # isort:skip

# FORMAT = "%(message)s"
# logging.basicConfig(
#     level=logging.DEBUG,
#     format=FORMAT,
#     datefmt="[%X,%f]",
#     handlers=[RichHandler(markup=True)],
# )

"""
Questions:
* What's the plan with the state
  - Distribution of helper functions? E.g. internal event creation?
"""


class InteractionLoopType(Enum):
    """The type of the interaction loop."""

    NEW = "new"  # Every new instance of the flow will live in its own new loop
    PARENT = "parent"  # Every new instance of the flow will live in the same loop as its the parent
    NAMED = "named"  # Every new instance of the flow will live in the loop with the given name


@dataclass
class FlowConfig:
    """The configuration of a flow."""

    # A unique id of the flow.
    id: str

    # The sequence of elements that compose the flow.
    elements: List[dict]

    # Interaction loop
    loop_id: Optional[str] = None
    loop_type: InteractionLoopType = InteractionLoopType.PARENT

    # The priority of the flow. Higher priority flows are executed first.
    # TODO: Check for what this is used exactly
    priority: float = 1.0

    # Whether it is an extension flow or not.
    # Extension flows can interrupt other flows on actionable steps.
    is_extension: bool = False

    # Whether this flow can be interrupted or not
    # TODO: Check for what this is used exactly
    is_interruptible: bool = True

    # Whether this flow is a subflow
    # TODO: Remove
    is_subflow: bool = False

    # The events that can trigger this flow to advance.
    # TODO: This will need to be dynamically determined based on current heads
    trigger_event_types = [
        "UserIntent",
        "BotIntent",
        "StartAction",
        "InternalSystemActionFinished",
    ]

    # The actual source code, if available
    source_code: Optional[str] = None


@dataclass
class FlowHead:
    """The flow head that points to a certain element in the flow"""

    # The unique id of a flow head
    uid: str

    # The position of the flow element the head is pointing to
    position: int

    # The flow of the head
    flow_state_uid: str

    # Matching score history of previous matches that resulted in this head to be advanced
    matching_scores: List[float]


class FlowStatus(Enum):
    """The status of a flow."""

    INACTIVE = "inactive"
    STARTING = "starting"
    STARTED = "started"
    ACTIVE = "active"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"
    COMPLETED = "completed"


@dataclass
class FlowState:
    """The state of a flow."""

    # The unique id of the flow instance
    uid: str

    # The name id of the flow
    flow_id: str

    # Interaction loop id
    loop_id: str

    # The position in the sequence of elements that compose the flow.
    # TODO: Generalize to have multiple heads for branching head statements like when/else
    head: FlowHead

    # The current set of variables in the flow state.
    context: dict

    # Parent flow id
    # TODO: Implement proper parenting
    parent_uid: Optional[str] = None

    # Child flow ids
    child_uids: List[str] = field(default_factory=list)

    # The current state of the flow
    status: FlowStatus = FlowStatus.INACTIVE

    # The UID of the flows that interrupted this one
    interrupted_by = None


@dataclass
class State:
    """A state of a flow-driven system."""

    # The current set of variables in the state.
    context: dict

    # The current set of flows in the state with their uid as key.
    flow_states: Dict[str, FlowState]

    # The configuration of all the flows that are available.
    flow_configs: Dict[str, FlowConfig]

    # Queue of internal events
    internal_events: Deque[dict] = deque()

    # The main flow state
    main_flow_state: FlowState = None

    # The next step of the flow-driven system
    next_steps: List[dict] = field(default_factory=list)

    # The comment is extract from the source code
    next_steps_comment: List[str] = field(default_factory=list)

    # The updates to the context that should be applied before the next step
    context_updates: dict = field(default_factory=dict)

    ########################
    # Helper data structures
    ########################

    # dictionary that maps from flow_id (name) to all available flow states
    flow_id_states: Dict[str, List[FlowState]] = field(default_factory=dict)

    def initialize(self) -> None:
        """
        Initialize the state to make it ready for the story start.
        """

        self.internal_events = deque()

        assert "main" in self.flow_configs, "No main flow found!"

        # Create flow states from flow config and start with head at position 0.
        self.flow_states = dict()
        for flow_config in self.flow_configs.values():
            flow_state = _create_new_flow_instance(self, flow_config)

            if flow_config.id == "main":
                if flow_config.loop_id is None:
                    flow_state.loop_id = create_readable_uuid("main")
                else:
                    flow_state.loop_id = flow_config.loop_id
                self.main_flow_state = flow_state


def _create_new_flow_instance(state: State, flow_config: FlowConfig) -> FlowState:
    loop_uid: Optional[str] = None
    if flow_config.loop_type == InteractionLoopType.NEW:
        loop_uid = str(uuid.uuid4())
    elif flow_config.loop_type == InteractionLoopType.NAMED:
        loop_uid = flow_config.loop_id
    # For type InteractionLoopType.PARENT we keep it None to infer loop_id at run_time from parent

    flow_uid = create_readable_uuid(flow_config.id)
    flow_state = FlowState(
        uid=flow_uid,
        context={},
        flow_id=flow_config.id,
        loop_id=loop_uid,
        head=FlowHead(
            uid=str(uuid.uuid4()),
            position=0,
            flow_state_uid=flow_uid,
            matching_scores=[],
        ),
    )

    state.flow_states.update({flow_state.uid: flow_state})
    if flow_state.flow_id in state.flow_id_states:
        state.flow_id_states[flow_state.flow_id].append(flow_state)
    else:
        state.flow_id_states.update({flow_state.flow_id: [flow_state]})

    return flow_state


def compute_next_state(state: State, external_event: dict) -> State:
    """
    Computes the next state of the flow-driven system.
    """
    logging.info(f"Process event: {external_event}")

    # Create a unique event time id to identify and track the resulting steps from the current event,
    # potentially leading to conflicts between actions
    external_event["matching_scores"] = []

    # Initialize the new state
    new_state = copy.copy(state)
    new_state.internal_events = deque([external_event])

    # Clear all matching scores
    for flow_state in state.flow_states.values():
        head = flow_state.head
        head.matching_scores.clear()

    heads_actionable: List[FlowHead] = []

    while new_state.internal_events:
        event = new_state.internal_events.popleft()
        logging.info(f"Process internal event: {event}")

        # Handle internal events that have no matchers in flows yet
        if event["type"] == "AbortFlow":
            flow_state = state.flow_states[event["flow_state_uid"]]
            _abort_flow(state, flow_state, event["matching_scores"])
        # elif event["type"] == "ResumeFlow":
        #     pass
        # elif event["type"] == "PauseFlow":
        #     pass

        # Find all heads of flows where event is relevant
        # TODO: Create a set to speed this up with all flow head related events
        heads_matching: List[FlowHead] = []
        heads_not_matching: List[FlowHead] = []
        match_order_score = 1.0

        for flow_state in state.flow_states.values():
            flow_config = state.flow_configs[flow_state.flow_id]
            # TODO: Generalize to multiple heads in flow
            head = flow_state.head

            element = flow_config.elements[head.position]
            if _is_match_element(element):
                # TODO: Assign matching score
                matching_score = _is_matching(new_state, element, event)
                if matching_score > 0.0:
                    # Make sure that we can always resolve conflicts, using the matching score
                    head.matching_scores = event["matching_scores"].copy()
                    matching_score *= match_order_score
                    match_order_score *= 0.99
                    head.matching_scores.append(matching_score)

                    heads_matching.append(head)
                    logging.info(f"Matching head (score: {matching_score}): {head}")
                else:
                    heads_not_matching.append(head)

        # Handle internal event matching
        for head in heads_matching:
            if event["type"] == "StartFlow":
                flow_state = _get_flow_state_from_head(state, head)
                flow_config = _get_flow_config_from_head(state, head)
                # Start flow
                flow_state.loop_id = state.flow_states[event["parent_flow_uid"]].loop_id
                # Initialize new flow instance of flow
                _create_new_flow_instance(new_state, flow_config)
            # TODO: Introduce default matching statements with heads for all flows
            # elif event["type"] == "AbortFlow":
            #     _abort_flow(state, flow_state)
            # elif event["type"] == "ResumeFlow":
            #     pass
            # elif event["type"] == "PauseFlow":
            #     pass

        # Abort all flows that had a mismatch when there is no other match
        # Not sure if we ever need this!
        # if not heads_matching:
        #     for head in heads_not_matching:
        #         flow_state = _get_flow_state_from_head(new_state, head)
        #         _abort_flow(new_state, flow_state, [])
        # return new_state

        heads_advancing = heads_matching
        while heads_advancing:
            # Advance front of all advancing heads ...
            heads_actionable = _advance_head_front(new_state, heads_advancing)
            # Now, all heads are either on a matching or an action (start action, send event) statement

            # Check for potential conflicts between actionable heads
            heads_advancing = []
            if len(heads_actionable) == 1:
                # If we have no or only one actionable head there are no conflicts
                heads_advancing = heads_actionable
                _record_next_step(new_state, heads_actionable[0])
            else:
                # Group all actionable heads by their flows interaction loop
                head_groups: Dict[str, List[FlowHead]] = {}
                for head in heads_actionable:
                    flow_state = _get_flow_state_from_head(new_state, head)
                    if flow_state.loop_id in head_groups:
                        head_groups[flow_state.loop_id].append(head)
                    else:
                        head_groups.update({flow_state.loop_id: [head]})

                # Find winning and loosing heads for each group
                for group in head_groups.values():
                    ordered_heads = _sort_heads_from_matching_scores(group)
                    winning_action = _get_flow_config_from_head(
                        new_state, ordered_heads[0]
                    ).elements[ordered_heads[0].position]

                    heads_advancing.append(ordered_heads[0])
                    _record_next_step(new_state, ordered_heads[0])
                    for head in ordered_heads[1:]:
                        if (
                            winning_action
                            == _get_flow_config_from_head(new_state, head).elements[
                                head.position
                            ]
                        ):
                            heads_advancing.append(head)
                        else:
                            flow_state = _get_flow_state_from_head(state, head)
                            _abort_flow(new_state, flow_state, head.matching_scores)

        # Now, all heads are on a match-statement, so let's process the next internal event

    return new_state


def _advance_head_front(state: State, heads: List[FlowHead]) -> List[FlowHead]:
    """
    Advances all provided heads to the next blocking elements (actionable or matching) and returns all heads on
    actionable elements.
    """
    heads_actionable: List[FlowHead] = []
    for head in heads:
        flow_state = _get_flow_state_from_head(state, head)
        flow_config = _get_flow_config_from_head(state, head)

        if flow_state.status == FlowStatus.INACTIVE:
            flow_state.status = FlowStatus.STARTING

        head.position += 1

        internal_events = slide(flow_state, flow_config, flow_state.head)
        flow_finished = flow_state.head.position >= len(flow_config.elements)
        state.internal_events.extend(internal_events)

        if flow_finished:
            logging.info(f"Flow {head.flow_state_uid} finished with last element")
        else:
            logging.info(
                f"Head in flow {head.flow_state_uid} advanced to element: {flow_config.elements[head.position]}"
            )

        if flow_finished or _is_match_element(flow_config.elements[head.position]):
            if flow_state.status == FlowStatus.STARTING:
                flow_state.status = FlowStatus.STARTED
                event = create_flow_started_internal_event(
                    flow_state.uid, head.matching_scores
                )
                state.internal_events.append(event)
        elif _is_actionable(flow_config.elements[head.position]):
            heads_actionable.append(head)

        # Check if flow has finished
        # TODO: Refactor to properly finish flow and all its child flows
        if flow_finished:
            _finish_flow(state, head)

    return heads_actionable


def _sort_heads_from_matching_scores(heads: List[FlowHead]) -> List[FlowHead]:
    score_lists = [(head.matching_scores, head) for head in heads]
    sorted_lists = sorted(score_lists, key=_custom_sort_key, reverse=True)
    return [e[1] for e in sorted_lists]


def _abort_flow(
    state: State, flow_state: FlowState, matching_scores: List[float]
) -> None:
    """Aborts a flow instance and all its active child flows."""
    # Generate FlowFailed event
    event = create_flow_failed_internal_event(flow_state.uid, matching_scores)
    state.internal_events.append(event)

    # abort all running child flows
    for child_flow_uid in flow_state.child_uids:
        child_flow_state = state.flow_states[child_flow_uid]
        event = create_abort_flow_internal_event(child_flow_state.uid, matching_scores)
        state.internal_events.append(event)

    flow_state.status = FlowStatus.ABORTED


def _finish_flow(state: State, head: FlowHead) -> None:
    """Finishes a flow instance and all active its child flows."""
    flow_state = _get_flow_state_from_head(state, head)

    # Generate FlowFinished event
    event = create_flow_finished_internal_event(flow_state.uid, head.matching_scores)
    state.internal_events.append(event)

    # Abort all running child flows
    for child_flow_uid in flow_state.child_uids:
        child_flow_state = state.flow_states[child_flow_uid]
        event = create_abort_flow_internal_event(
            child_flow_state.uid, head.matching_scores
        )
        state.internal_events.append(event)

    flow_state.status = FlowStatus.COMPLETED


def _get_head_element_from_head(state: State, head: FlowHead) -> dict:
    """Returns the element at the flow head position"""
    return _get_flow_config_from_head(state, head).elements[head.position]


def _get_flow_config_from_head(state: State, head: FlowHead) -> FlowConfig:
    """Returns the flow config of the flow of the head"""
    return state.flow_configs[_get_flow_state_from_head(state, head).flow_id]


def _get_flow_state_from_head(state: State, head: FlowHead) -> FlowState:
    """Returns the flow state of the flow head"""
    return state.flow_states[head.flow_state_uid]


# Define a custom sorting key function for pairwise comparisons
def _custom_sort_key(input_list):
    return tuple(input_list)


def _is_actionable(element: dict) -> bool:
    """Checks if the given element is actionable."""
    return element["_type"] == "run_action"


def _is_match_element(element: dict) -> bool:
    return element["_type"] == "match_event"


# TODO: Refactor this
def _is_matching(state: State, element: dict, event: dict) -> float:
    """Checks if the given element matches the given event."""

    FUZZY_MATCH_FACTOR = 0.5

    # The element type is the first key in the element dictionary
    element_type = element["_type"]

    if event["type"] == "StartFlow":
        return float(
            element_type == "match_event"
            and element["type"] == "StartFlow"
            and element["flow_id"] == event["flow_id"]
        )
    if event["type"] == "FlowStarted":
        return float(
            element_type == "match_event"
            and element["type"] == "FlowStarted"
            and element["flow_id"] == state.flow_states[event["flow_state_uid"]].flow_id
        )
    elif element["_type"] == "UserIntent":
        return float(
            element_type == "UserIntent"
            and (
                element["intent_name"] == "..." * FUZZY_MATCH_FACTOR
                or element["intent_name"] == event["intent"]
            )
        )

    elif event["type"] == "BotIntent":
        return float(
            element_type == "start_action"
            and element["action_name"] == "utter"
            and (
                element["action_params"]["value"] == "..." * FUZZY_MATCH_FACTOR
                or element["action_params"]["value"] == event["intent"]
            )
        )

    elif event["type"] == "InternalSystemActionFinished":
        # Currently, we only match successful execution of actions
        if event["status"] != "success":
            return False

        return (
            element_type == "start_action"
            and element["action_name"] == event["action_name"]
        )

    elif event["type"] == "StartUtteranceBotAction":
        return float(
            element_type == "StartUtteranceBotAction"
            and (
                element["script"] == "..." * FUZZY_MATCH_FACTOR
                or element["script"] == event["script"]
            )
        )

    else:
        # Its an UMIM event
        if element_type != "match_event":
            return float(False)

        # Match the event by type explicitly, and all the properties.
        if event["type"] != element["type"]:
            return float(False)

        # We need to match all properties used in the element. We also use the "..." wildcard
        # to mach anything.

        score = 1.0
        for key, value in element.items():
            # Skip potentially private keys.
            if key.startswith("_"):
                continue
            if value == "...":
                score *= FUZZY_MATCH_FACTOR
            if event.get(key) != value:
                return float(False)

        return score


def _record_next_step(
    state: State,
    head: FlowHead,
) -> None:
    """Helper to record the next step."""
    element = _get_head_element_from_head(state, head)
    if _is_actionable(element):
        state.next_steps.append(element)
        # Extract the comment, if any
        state.next_steps_comment = element.get("_source_mapping", {}).get("comment")


def _step_to_event(step: dict) -> dict:
    """Helper to convert a next step coming from a flow element into the actual event."""
    step_type = step["_type"]

    if step_type == "StartAction":
        if step["action_name"] == "utter":
            return {
                "type": "BotIntent",
                "intent": step["action_params"]["value"],
            }

        else:
            action_name = step["action_name"]
            action_params = step.get("action_params", {})
            action_result_key = step.get("action_result_key")

            return new_event_dict(
                "StartInternalSystemAction",
                action_name=action_name,
                action_params=action_params,
                action_result_key=action_result_key,
            )
    else:
        raise ValueError(f"Unknown next step type: {step_type}")


# NOTE (schuellc): Are we going to replace this with a stateful approach
def compute_next_steps(
    history: List[dict], flow_configs: Dict[str, FlowConfig]
) -> List[dict]:
    """Computes the next step in a flow-driven system given a history of events."""
    state = State(context={}, flow_states=[], flow_configs=flow_configs)

    # First, we process the history and apply any alterations e.g. 'hide_prev_turn'
    actual_history = []
    for event in history:
        # NOTE (schuellc): Why is this needed?
        if event["type"] == "hide_prev_turn":
            # we look up the last `UtteranceUserActionFinished` event and remove everything after
            end = len(actual_history) - 1
            while (
                end > 0 and actual_history[end]["type"] != "UtteranceUserActionFinished"
            ):
                end -= 1

            assert actual_history[end]["type"] == "UtteranceUserActionFinished"
            actual_history = actual_history[0:end]
        else:
            actual_history.append(event)

    for event in actual_history:
        state = compute_next_state(state, event)

        # NOTE (Jul 24, Razvan): this is a quick fix. Will debug further.
        if event["type"] == "bot_intent" and event["intent"] == "stop":
            # Reset all flows
            state.flow_states = []

    next_steps = []

    # If we have context updates after this event, we first add that.
    if state.context_updates:
        next_steps.append(new_event_dict("ContextUpdate", data=state.context_updates))

    # If we have a next step, we make sure to convert it to proper event structure.
    for step in state.next_steps:
        next_step_event = _step_to_event(step)

        next_steps.append(next_step_event)

    # Finally, we check if there was an explicit "stop" request
    if actual_history:
        last_event = actual_history[-1]
        # NOTE (schuellc): Why is this needed?
        if last_event["type"] == "BotIntent" and last_event["intent"] == "stop":
            # In this case, we remove any next steps
            next_steps = []

    return next_steps


def compute_context(history: List[dict]):
    """Computes the context given a history of events.

    # We also include a few special context variables:
    - $last_user_message: the last message sent by the user.
    - $last_bot_message: the last message sent by the bot.
    """
    context = {
        "last_user_message": None,
        "last_bot_message": None,
    }

    for event in history:
        if event["type"] == "ContextUpdate":
            context.update(event["data"])

        if event["type"] == "UtteranceUserActionFinished":
            context["last_user_message"] = event["final_transcript"]

        elif event["type"] == "StartUtteranceBotAction":
            context["last_bot_message"] = event["script"]

    return context