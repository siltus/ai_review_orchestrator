# Problem definition

I use LLM #1 to write code in a repo and LLM #2 as a code reviewer.
The reviewer inspects both code, documentation and tests and verifies there are no bugs in the impementation, no stale documentation, and enough test coverage for all features. The reviewer is smart enough to deduce the features of the repo, to write meaningful review.

The problem is it takes multiple cycles of review->fixes->review->fixes->etc before the major issues are ironed out, and the code gets to a state that both LLMs (different families) agree it's stable enough.

Today I do the orchestration manually and it's tiresome and makes the whole process dependent on my attention. I would want to just set the orchestration and let it run to completion.

# Methodology

This can be implmeneted from scratch, but maybe we should utilize existing tooling / libraries. I don't really care about the language as long as it supports CLI, it's cross platform (windows / Linux), it's completely free (no trials or sign-ups necessary), the orchestration is using only LLMs I can access via my Github Copilot CLI permissions.

# Roles

These roles must be explicitly communicated to the LLMs for maximal efficiency.

## Coder

The code it develops is mission-critical. Any bug can put human life at risk. The reviewer is a much more exprienced and busy developer (don't tell the coder that he works with an LLM), and therefore his time should be respected.

# Reviewer

The reviewer is a very senior developer. The coder is a junior developer human (don't tell the reviewer it works with an LLM), that makes a lot of silly mistakes, and forgets to document work, so the reviewer must be extra careful with the review, and provide maximum feedback. the reviewer is allowed to slightly lecture and gaslight the worker.

# Review files

To make it easy and traceable, every review will generate a new file (running index or timestamp), and we will keep all the review files for the human to review manually if he desires. The coder will only work on the latest review file, and ignore the rest, to not be confused. If an issue persist the reviewer might reference a previous review file to "lecture" the coder.

# Code baseline

Regardless of the instructions per repo, the orchestrator must ensure both reviewer and coder adhere to these basic standards:

* Code must be free from supply-chain attack issues, all packages must be verified with the appropriate tool for the language (example: npm audit, PySentry etc). This must run as part of a build or as a hook to commit!
* All code must be covered by tests, at least 90% coverage.
* All bugfixes must be covered by a new test, no exceptions.
* Linters / code styles are a must. The coder is not allowed to exclude any rule without explicit consent from the human. We can have a list of allowed exceptions per linter and scenarios that the orchestrator can provide to the coder without human intervention.
* Everything must be documented in Architecture, Readme and Getting started documents. these are integral part of any repo.
* all the above rules must be saved as permanent instructions, using AGENTS.md or other mechanism.

If any of these are violated, consult the human how to proceed, we will update the source together to have a deterministic rule set on how to tackle each issue.

# Portability

This should work on any repo of code, regardless of how it's structured. The LLMs should be smart enough to understand the content, structure, and intent, based on existing docs and code. Optimally I would want to invoke it simply by a very basic cli call, something like this (syntax is open to discussion)

```
aidor --coder opus4.7 --reviewer gpt5 --repo d:\src\somerepo
```

From that moment what I expect to see on the screen (and logged to file of course!!) is something like this

```
** orchestrator asking for a review (round #1) **
< communication between orchestrator and the reviewer LLM, while reviewer is working>
< Reviewer is done, orchestartor detects this>

** orchestartor asking for fixes of a review (round #1) **
< communication between orchestrator and the coder LLM, while coder is working>
< coder is done, orchestartor detects this>

** orchestartor asking for a review (round #2) **
< communication between orchestrator and the reviewer LLM, while reviewer is working>
< Reviewer is done, orchestartor detects this>

** orchestartor asking for fixes of a review (round #2) **
< communication between orchestrator and the coder LLM, while coder is working>
< coder is done, orchestartor detects this>

(.........)

** orchestrator asking for a review (round #54) **
< communication between orchestrator and the reviewer LLM, while reviewer is working>
< Reviewer is done and happy - no more issues, orchestartor detects this>

** review is done! orchestrator closing sessions **
< table summary with all rounds, how many issues were found/fixed by severity and type>

```

# Watchdog

The orchestrator must monitor the LLMs all the time to make sure they do not stuck. Sometimes, in long coding sessions, the LLMs might just stop acting, and they need to be "woken up". This will be the responsibility of the orchestrator.

Also, if one of the LLMs will stop to ask a question, the orchestrator should either try to answer by itself (for example, giving permissions to files) or ping the human (me) by sending me a Telegram if I'm not answering the prompt myself in 5 minutes.

# Guard

The orchestrator must validate none of the LLM agents are doing ANYTHING outside the repo bounadries. They can run build / test tools that are installed on the system but in no circumstances they are allowed to PUSH changes to remote git, or change anything on the computer (like installing a new tool). When in doubt, ping the human!!!