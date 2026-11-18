# Quarterdeck: the five-minute overview

The first open-source project from **Navigate AI**, the organisation that builds
and maintains it. This page explains Quarterdeck quickly
and gives ready-to-use text for sharing it.

---

## The name

The **quarterdeck** is where a ship's captain stands and gives the orders.
In Quarterdeck you are the captain: a crew of AI agents does the work, and
every decision that matters stays with you. It also fits Navigate AI's own
name.

---

## In one line

**Your own team of AI agents, on your own Mac, running on the model providers
you choose, and working the way you set it up.**

Other taglines:

- A private AI office for one person, on one Mac.
- Your models. Your Mac. Your rules.
- An AI team that asks before it acts.

**GitHub description** (under 350 characters):

> A personal working agent for one person on their own Mac. A lead agent and a
> team of AI colleagues research, write and build using the model APIs you
> choose, within budgets you set, and ask before they act. Shape it with your
> own coding agent: Codex, Claude Code or opencode. Apache-2.0.

---

## The 30-second pitch

Most AI assistants live in someone else's cloud, use someone else's model
choices and forget your way of working between chats. **Quarterdeck** runs on
your own Mac instead. You bring your own API keys from Anthropic, OpenAI,
DeepSeek, Kimi, Qwen or any OpenAI-compatible provider. You decide which model
does which job, what the agents may read, how much they may spend and when
they must ask you. A lead agent handles everyday questions. For bigger work, a
team plans, researches, criticises and writes. When work needs code, the
office hands it to the coding CLI already on your Mac and reviews the result.
And because the office is open source on your machine, you can change it with
the same coding agents.

---

## Why it exists

Working with AI every day means dealing with:

- **Scattered tools.** One chat for research, another for writing, a terminal
  for code, and nothing that remembers the project.
- **No control over models or cost.** The provider picks the model, and spend
  is only visible at the end of the month.
- **Agents that act first and explain later.** Autonomy is great until
  something is sent, spent or published that you didn't want.
- **Your context on someone else's server.** Documents, history and decisions
  leave your machine.

Quarterdeck puts one office on your own computer: a lead and a team of agents
that know your projects, use your models and stay inside the limits you set.

---

## What makes it different

| | |
|---|---|
| **Local and private** | Runs in Docker on your Mac and answers only that computer by default. Your data stays in your own database, with backup, restore and export built in. |
| **Bring your own models** | Any OpenAI-compatible provider, plus Anthropic. Each role can use its own model, each provider can have a backup, and model prices are read from a live catalogue. |
| **Budgets you can see** | Limits per task, project and month, with live spend and warnings at 80% and 100%. Work can finish on a cheaper model. |
| **Asks before it acts** | Building, running code, publishing, spending or widening access waits for your press. Answers state what they could not verify. |
| **Your own coding agents** | Code runs through Claude Code, Codex, opencode or DeepSeek Harness on your Mac, with your own plan, in their own sandbox, on a separate branch. The office checks the result, and nothing reaches your branches without your merge. |
| **Configurable to how you work** | Choose the colleagues, their models, rules and skills; which folders, tools (MCP servers) and calendars they may use; recurring work like a morning recap; and Slack or your phone as extra doors in. |
| **Open and changeable** | Plain Python and JavaScript, Apache-2.0, with tests and decision records. Point your coding agent at it and make it yours. |

---

## How it works

1. **Ask.** Type in the office (or in Slack). The lead agent answers or does
   small work straight away, showing each tool it used.
2. **Start.** For bigger work, the office offers a plan with a cost estimate.
   It begins only when you press **Start**.
3. **Work.** A team researches the web and your granted documents, a critic
   reviews, and a writer produces the answer as a document you can download
   as Word, Excel, slides or a web page. Code goes to your coding CLI.
4. **Decide.** Anything that would build, publish, spend or widen access
   waits in your in-tray. You approve, send it back or stop it.

---

## A day with Quarterdeck

- **08:00:** A recurring morning recap is waiting: what finished overnight,
  what needs you, today's calendar.
- **10:30:** You ask, "Compare the three main options for our next vendor, on
  price and coverage." The office proposes a plan for about $0.40. You press
  Start, and an hour later there is a sourced report with its open questions
  named.
- **14:00:** In a project discussion you ask for a small landing page. The
  build goes to Claude Code on your Mac. You watch its steps, the office runs
  the checks, and you preview it before merging.
- **17:00:** From your phone, over Tailscale, you approve one waiting item and
  park another for next week.

---

## Who it is for

**Good fit:** one person (a founder, consultant, researcher or developer) who
works on a Mac, already has API keys or coding-agent plans, and wants one
private place where AI work is organised, budgeted and approved.

**Not a fit yet:** teams sharing one office, hosted or SaaS use, Windows or
Linux desktops as the main machine, or anyone expecting a signed one-click
app.

---

## Get started

You need a Mac with Docker Desktop and Python 3.9 or later. A coding CLI is
optional, for building.

```sh
git clone https://github.com/NavigateAI-dev/quarterdeck.git && cd quarterdeck
python3 -m scripts.launch
```

The launcher checks Docker, creates a private `.env` and key, starts the
services and opens the office already signed in. Then add a model provider
under **Connections** and ask your first question. Details are in the
[README](../README.md#install).

---

## Honest limits

- It is a first public release, used daily inside Navigate AI but not yet
  installed by anyone outside it. Expect rough edges.
- It is Mac-first. The calendar helper, the coder on the Mac and the folder
  picker are macOS only.
- You pay your model providers directly. The office shows and limits spend,
  but it can't make a model cheaper.
- It is for one owner. There are no multi-user accounts or roles.

---

## FAQ

**Does my data leave my Mac?**
Only what you send to the model providers you connect, plus the web pages and
tools the agents use for a task. The office itself is not hosted anywhere, and
it can keep your AI history out of chosen providers and out of backups.

**Which models can I use?**
Any OpenAI-compatible API, including OpenAI, DeepSeek, Kimi, Qwen, GLM,
MiniMax, OpenRouter and SiliconFlow, plus Anthropic. Each role can use a
different one.

**Can an agent run code or publish on its own?**
No. Code runs only after your approval, in your own coding CLI's sandbox, on
a separate branch. Publishing a website needs your press and a confirmation
that it will be public.

**Can I change how it works?**
Yes. Colleagues, rules, skills, tools and limits are settings. Beyond that,
the source is yours: open it in Codex, Claude Code or opencode and change it.

**What does it cost?**
The software is free under Apache-2.0. You pay only for the model and search
APIs you connect, within the budgets you set.

---

## Ready-to-post text

**Short post (X, Threads, Mastodon):**

> Meet Quarterdeck, the first open-source project from Navigate AI: a personal
> AI team that runs on your own Mac, uses the model APIs you choose, stays
> inside your budgets and asks before it acts. Shape it with Codex, Claude
> Code or opencode. Apache-2.0.

**Longer post (LinkedIn, a blog or a newsletter):**

> Today **Navigate AI** opens its first project to the community:
> **Quarterdeck**. We built it for people who want one place for their AI work
> that they fully control.
>
> It's a personal working agent for one person on their own Mac. A lead agent
> answers everyday questions. For bigger work, a team plans, researches,
> criticises and writes, using the model providers *you* choose (Anthropic,
> OpenAI, DeepSeek, Kimi, Qwen and more), within budgets *you* set. Nothing is
> built, published or spent without your approval. When work needs code, it
> goes to the coding CLI already on your Mac, and the office reviews the
> result before anything is merged.
>
> It's Mac-first, early, and honest about its limits. If you like to work
> locally and shape your own tools, try it, change it with your own coding
> agent, and tell us what you'd want next: issues and pull requests are
> welcome on Navigate AI's GitHub.

**中文简介（微信、小红书、即刻）:**

> **Quarterdeck：Navigate AI 的第一个开源项目。**
> 一个只属于你的 AI 工作团队，运行在你自己的 Mac 上。模型由你选：Anthropic、
> OpenAI、DeepSeek、Kimi、通义千问等都可以，用你自己的 API。预算由你定，
> 关键操作要先经过你批准。需要写代码时，交给你 Mac 上的 Claude Code、Codex
> 或 opencode 来做，结果先经过检查再合并。项目以 Apache-2.0 开源，可以用你
> 自己的编程助手随意修改。适合喜欢在本地工作、想按自己的方式搭建工作环境的个人用户。
