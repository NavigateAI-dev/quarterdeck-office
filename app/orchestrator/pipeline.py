"""Assemble the analysis pipeline from its nodes."""

from collections.abc import Callable, Hashable
from functools import partial
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.judgments import Judge
from app.orchestrator.engineering import EngineeringExecutor
from app.orchestrator.graph import (
    ANALYSIS_WORKFLOW,
    RESEARCH_WORKFLOW,
    STOP_NODE,
    WORKFLOWS,
    AdviceMemory,
    AgentSet,
    ApprovalGate,
    TeamBoard,
    building,
    proposes_action,
)
from app.orchestrator.nodes.context import NodeContext
from app.orchestrator.nodes.finish import approval, engineering, synthesis
from app.orchestrator.nodes.planning import after_plan, after_replan, plan, replan
from app.orchestrator.nodes.review import continue_or_stop, critique, decide, stopped
from app.orchestrator.nodes.work import after_work, research, strategy, work
from app.orchestrator.state import AnalysisState, Node


def build_graph(
    agents: AgentSet,
    max_revisions: int,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    gate: ApprovalGate | None = None,
    executor: EngineeringExecutor | None = None,
    judge: Judge | None = None,
    guidance: Callable[[str], list[str]] | None = None,
    max_parallel: int = 4,
    workflow: str = ANALYSIS_WORKFLOW,
    advice_memory: "AdviceMemory | None" = None,
    board: "TeamBoard | None" = None,
    team: bool = False,
    teammate: Callable[[Any, str], Any] | None = None,
    late_notes: Callable[[str], list[str]] | None = None,
    skipped: Callable[[], set[str]] | None = None,
    prioritized: Callable[[], list[str]] | None = None,
) -> CompiledStateGraph[AnalysisState, Any, AnalysisState, AnalysisState]:
    """Assemble the fixed V0 analysis chain.

    The research-only workflow (a task given to the researcher from chat) is
    the same chain without the strategy step: plan, research, critique,
    synthesis. It proposes nothing to execute, so it ends without approval.

    Revising research invalidates the strategy and critique that were derived
    from it; revising strategy invalidates only the critique. Stale artifacts
    are cleared rather than carried forward.
    """
    if workflow not in WORKFLOWS:
        raise ValueError(f"unknown workflow: {workflow}")
    research_only = workflow == RESEARCH_WORKFLOW
    ctx = NodeContext(
        agents=agents,
        max_revisions=max_revisions,
        checkpointer=checkpointer,
        gate=gate,
        executor=executor,
        judge=judge,
        guidance=guidance,
        max_parallel=max_parallel,
        workflow=workflow,
        advice_memory=advice_memory,
        board=board,
        team=team,
        teammate=teammate,
        late_notes=late_notes,
        skipped=skipped,
        prioritized=prioritized,
        research_only=research_only,
    )
    builder: StateGraph[AnalysisState, Any, AnalysisState, AnalysisState] = StateGraph(
        AnalysisState
    )
    nodes: dict[str, Any] = {
        Node.PLAN: partial(plan, ctx),
        Node.RESEARCH: partial(research, ctx),
        Node.STRATEGY: partial(strategy, ctx),
        Node.CRITIQUE: partial(critique, ctx),
        Node.SYNTHESIS: partial(synthesis, ctx),
        Node.WORK: partial(work, ctx),
        Node.REPLAN: partial(replan, ctx),
        STOP_NODE: partial(stopped, ctx),
    }
    if research_only:
        del nodes[Node.STRATEGY]
    for name, function in nodes.items():
        builder.add_node(str(name), function)
    builder.add_edge(START, Node.PLAN)
    builder.add_conditional_edges(
        Node.PLAN,
        partial(after_plan, ctx),
        {
            Node.RESEARCH: Node.RESEARCH,
            Node.WORK: Node.WORK,
            Node.SYNTHESIS: Node.SYNTHESIS,
            STOP_NODE: STOP_NODE,
        },
    )
    builder.add_conditional_edges(
        Node.WORK,
        partial(after_work, ctx),
        {
            Node.WORK: Node.WORK,
            Node.REPLAN: Node.REPLAN,
            Node.CRITIQUE: Node.CRITIQUE,
            STOP_NODE: STOP_NODE,
        },
    )
    builder.add_conditional_edges(
        Node.REPLAN,
        partial(after_replan, ctx),
        {
            Node.WORK: Node.WORK,
            Node.CRITIQUE: Node.CRITIQUE,
            Node.SYNTHESIS: Node.SYNTHESIS,
            STOP_NODE: STOP_NODE,
        },
    )
    after_research = Node.CRITIQUE if research_only else Node.STRATEGY
    builder.add_conditional_edges(
        Node.RESEARCH,
        continue_or_stop(ctx, after_research),
        {after_research: after_research, STOP_NODE: STOP_NODE},
    )
    revisions: dict[Hashable, str] = {
        Node.SYNTHESIS: Node.SYNTHESIS,
        Node.RESEARCH: Node.RESEARCH,
        Node.REPLAN: Node.REPLAN,
        STOP_NODE: STOP_NODE,
    }
    if not research_only:
        builder.add_conditional_edges(
            Node.STRATEGY,
            continue_or_stop(ctx, Node.CRITIQUE),
            {Node.CRITIQUE: Node.CRITIQUE, STOP_NODE: STOP_NODE},
        )
        revisions[Node.STRATEGY] = Node.STRATEGY
    builder.add_conditional_edges(Node.CRITIQUE, partial(decide, ctx), revisions)
    if gate is None:
        builder.add_edge(Node.SYNTHESIS, END)
    else:
        builder.add_node(str(Node.APPROVAL), partial(approval, ctx))
        builder.add_node(str(Node.ENGINEERING), partial(engineering, ctx))
        # An approval authorises something; an answer that proposes nothing
        # is finished. A synthesis from before `proposes_action` was asked
        # is read as it was then: a scope meant a proposal.
        builder.add_conditional_edges(
            Node.SYNTHESIS,
            # A research-only task proposes nothing, unless it is a build.
            lambda state: (
                Node.APPROVAL
                if proposes_action(state) and (not research_only or building(state))
                else END
            ),
            {Node.APPROVAL: Node.APPROVAL, END: END},
        )
        builder.add_edge(Node.APPROVAL, Node.ENGINEERING)
        builder.add_edge(Node.ENGINEERING, END)
    builder.add_edge(STOP_NODE, END)
    return builder.compile(checkpointer=checkpointer)
