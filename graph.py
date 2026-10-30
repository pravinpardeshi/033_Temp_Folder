"""LangGraph pipeline for agent routing.

Graph shape:

    START -> router -> (snow | rca | solution) -> END

- ``router`` decides which agent handles the request. It honors an explicit
  ``requested_agent`` (from the API payload), otherwise it asks an LLM to
  classify the intent when more than one agent is registered.
- Each agent is a node in the graph. Add a node + edge to extend the pipeline.

The graph is built from the orchestrator's agent registry, so registering a
new agent automatically adds it to the pipeline.
"""

from typing import Any, Callable, Dict, Optional, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.base import BaseAgent


class AgentState(TypedDict, total=False):
    session_id: str
    prompt: str
    requested_agent: Optional[str]
    selected_agent: str
    answer: str
    error: str


AgentRegistry = Dict[str, Tuple[BaseAgent, str]]


def _make_agent_node(key: str, agent: BaseAgent) -> Callable[[AgentState], Any]:
    async def node(state: AgentState) -> AgentState:
        try:
            answer = await agent.invoke(state["session_id"], state["prompt"])
            return {"answer": answer}
        except Exception as e:  # noqa: BLE001 - surfaced to the caller
            return {"answer": f"Error invoking {key} agent: {e}", "error": str(e)}

    node.__name__ = f"{key}_node"
    return node


def build_graph(
    agents: AgentRegistry,
    default_key: str,
    classify_intent: Callable[[str], str],
) -> Any:
    """Compile the routing graph for the given agent registry."""

    def route(state: AgentState) -> AgentState:
        requested = state.get("requested_agent")
        if requested in agents:
            return {"selected_agent": requested}
        if len(agents) == 1:
            return {"selected_agent": default_key}
        return {"selected_agent": classify_intent(state["prompt"])}

    graph = StateGraph(AgentState)
    graph.add_node("router", route)

    for key, (agent, _description) in agents.items():
        graph.add_node(key, _make_agent_node(key, agent))

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        lambda state: state["selected_agent"],
        {key: key for key in agents},
    )
    for key in agents:
        graph.add_edge(key, END)

    return graph.compile()


async def run_graph(graph: Any, session_id: str, prompt: str, agent_key: Optional[str] = None) -> str:
    result = await graph.ainvoke({
        "session_id": session_id,
        "prompt": prompt,
        "requested_agent": agent_key,
    })
    return result.get("answer", "No response received from the agent.")
