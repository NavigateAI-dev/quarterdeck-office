# 0001. The lead agent answers; the full team is one press away

Date: 15 Oct 2026, amended 23 and 27 Oct 2026 · Status: Accepted · Decided by: the owner

## Context
Every request went through the full team: a planner, plan reviews, waves of researchers, a
critic and the Chief. Simple questions were slow, often ran out of budget, and were answered no
better than a single capable model would. DeepSeek Harness, on the same providers, was quick on
simple and complex work alike, because one model sized its own effort.

## Decision
Every request goes first to one **lead agent**: a bounded native-tool loop that looks things up,
keeps a checklist, delegates parallel research, asks for a review and proposes a team run when the
work is large. The team pipeline (LangGraph, plan in waves) stays for large work the owner starts.
On 23 Oct, after real use showed no task ever reached the lead (every task came from a
discussion's Start, which always used the team), Start goes to the lead as well; "Redo with full
team" keeps the team. The lead never starts the team on its own: it proposes one, and the owner
starts it with a button on the task or under the reply.

## Consequences
Answers cost a few calls instead of a dozen or more. The lead's quality is watched on Settings →
Performance. On 27 Oct, after ten days in which the full team ran about once a week, the owner
retired the "team" setting and the one-shot tool choice that only it used; the full team stays
one tick (Full team on a task) or one press (Redo with full team, Start the team run) away.
Slack keeps its planning path for a message with a shared picture, which the lead cannot see.
