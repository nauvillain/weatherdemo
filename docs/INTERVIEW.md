# Talking about this as a manager

You are interviewing to lead four infrastructure engineers, so the project matters
less as code than as proof of judgment: you picked a real problem, bounded the risk,
measured the result, and know what you would do differently. Lead with decisions.

## The two-minute version

"I built a small copy of the platform shape: a Databricks pipeline on Azure feeding a
private origin, served through Cloudflare on my own domain. Then I added an AI agent
to investigate failures. The agent does not decide whether something is wrong;
ordinary threshold checks do that. It is only asked why, it can only read, and the
two fixes it can propose need a human yes. To find out whether it was any good, I
wrote ten failure drills with known causes and scored it. It got N of 10. The ones it
missed were X, and here is what that told me."

Fill in N and X from your scorecard. A specific miss you understand is more
convincing than a perfect score.

## Decisions worth explaining

| Decision | Why | What it shows |
|---|---|---|
| Rules detect, agent diagnoses | Detection must be cheap, fast, and never wrong in a new way. Diagnosis is where reading logs across systems costs engineers time. | You put the tool where the toil is |
| Read-only by default, actions behind approval | A wrong diagnosis wastes minutes. A wrong action causes an outage. | You separate reversible from irreversible |
| Evaluate before trusting | Without drills, "the agent works" is an anecdote. | You manage by evidence |
| Hide the answer key from the agent | Job parameters and stack traces reveal which drill is running, so the tools strip them. Otherwise the agent scores 10 of 10 and the number means nothing. | You know how evaluations fool you |
| Private origin, separate read and write tokens | The edge and the agent can only read. The pipeline can only write. | Least privilege as a habit |
| Serve stale when the origin fails, and say so | Users keep getting data; the `x-cache: STALE` header keeps the problem visible. | Availability without hiding failures |
| Hard step limit and token accounting | A confused agent must stop, and you need the cost per investigation. | You think about run cost |
| Two Terraform stacks, schedule paused | Controls when the trial clock and the bill start. | Cost awareness |

## How you would roll this out with a team of four

1. **Start where it cannot hurt.** Read-only, on one pipeline, posting to a channel next to the existing alert. Nobody is paged by it.
2. **Build the drill set from your own incident history.** The last ten postmortems are the first ten test cases.
3. **Set the bar before looking at results.** For example: correct on eight of ten, never confidently wrong on the control case.
4. **Give it its own identity.** A service principal with SELECT on specific tables and an analytics-read API token. The code-level guards in this repo are a backstop, not the control.
5. **Add actions one at a time**, each with approval, an audit trail, and a rollback, and only for fixes the team already does by hand without thinking.
6. **Assign an owner.** Prompts and tool descriptions drift like any other code. Someone reviews the scorecard when the model or the platform changes.

Metrics to propose: time from alert to correct diagnosis, share of investigations
where the engineer agreed with the agent, false alarm rate on healthy checks, and
cost per investigation.

## Limits to state before they ask

- Six cities and a handful of requests a day. Nothing here proves behaviour at scale.
- The drills are faults you invented, so they are easier than faults nobody predicted.
- The agent sees logs and probes, not code or recent deploys. Connecting it to change history is the obvious next step and often the real cause.
- You built this in two weeks on a trial. Say what you did not get to.

## Questions to ask them

- Where does on-call time actually go today: detection, diagnosis, or the fix?
- How does a pipeline change reach production, and how does the CDN configuration?
- Who owns data freshness end to end, across the pipeline team and the edge team?
- What is the team's current appetite, and policy, for AI tools touching production systems?
