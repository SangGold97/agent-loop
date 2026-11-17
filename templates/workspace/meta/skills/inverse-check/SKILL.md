---
name: inverse-check
description: Test a conclusion by trying to prove its opposite, then observe the one premise that decides between them. Use it before you state, record or act on a conclusion that you or others will rely on, even when it seems obvious - a verdict that something works, fails, is clean, is ruled out or is not worth doing; a forecast of time, cost, score or odds; a claimed cause; a comparison that keeps or drops an option; a recommendation or an outward step such as a message, merge, stop, submission or closing report - and whenever someone asks whether you are sure. Skip it for routine progress, numbers you are only restating, and small steps that are easy to undo.
---

# Inverse check

Most wrong conclusions follow sound reasoning from a premise nobody looked at: a default taken
for the real setting, a figure from someone's summary, a pace read without the limit that ends
it. Whoever reached the conclusion is the least likely to see that premise, because the
reasoning around it looks right. This check finds the premise by arguing against yourself, and
settles it by looking, not by arguing more. It is not a general review of your work; you
already check work as you go. Use it at the moment a conclusion is about to be relied on.

## The check

State the claim your next step rests on in one sentence. Assume it is false and make the
strongest case for that, as someone who wanted it to be false would. Then find the premise that
decides between the two cases; it is usually one you inferred, took from a default or a
document, or read in someone's summary. Observe it at its source - the running system, the
data, the scorer, the code path that actually runs - with a tool result from this session. A
fact counts as established when someone observed it, not when it was stated: a summary, a plan
line and your own earlier message are statements.

Keep, revise or reverse the claim on what you observed. Reverse only on a new observation,
because a reversal from doubt alone is the same error in the other direction. A check that
produced no new observation did not happen. If the observation is within reach, make it now;
if it needs a long run, start it and treat the claim as open until the result returns, while
you continue the work that does not depend on it. The check needs nobody's approval: run it,
then act on what it showed.

When the claim goes into a plan, a memory entry, a brief or a report, say how it stands: what
you observed and how, or that it is still open and the test that would settle it. A verdict
that rests on arithmetic, a paper, a document or someone's summary stays open until its
premises are observed, and a result you pass on keeps the scope and caveats its source gave.

## Where each kind of claim usually breaks

**Forecasts** (when, how much, how likely). An experienced planner first asks what ends or
blocks the process - a timeout, quota, deadline, budget, queue or dependency - and only then
reads its pace, because a correct pace carried past a limit gives a confident wrong answer.
They compare with how long similar work actually took, and ask what would make the estimate
wrong by half or double. Give a range and the observation that would show it wrong.

**Negative verdicts** (fails, ruled out, floor, not worth it, not needed, clean). An experimental
scientist knows that a failed attempt closes that attempt, not the direction, and that a bound
closes a direction only when every premise behind it has been observed. Before rejecting an
idea, build its strongest version; where others got past the limit, find what they did
differently. Ask what a solution beyond the floor would have to do that none of the tried ones
does - that is a new hypothesis, not a reason to stop. A check that finds nothing ("clean",
"none left", "passed") is only as good as the tool that ran it: before trusting the zero, show
that the tool finds a case known to be there. A wrong negative ends all later work on the
direction, so it is the most expensive mistake and the one that most needs this check.

**Diagnoses** (X causes Y). A physician or incident investigator keeps at least two causes in
mind and looks for the observation that tells them apart, because a signal that matches a
known failure may have a different cause. Removing the supposed cause - revert, ablate,
replay - is the strongest test, and the case that does not fit deserves the most attention.

**Comparisons** (A beats B; keep or drop). A statistician asks whether the difference is larger
than the noise (paired errors, reruns, seeds), whether both sides ran under the same conditions
(data, version, cache, settings), and whether the evaluation data shaped the change. A gain
that holds only on the data it was tuned on is not yet a gain.

**Recommendations and outward steps** (message, stop, merge, submit, close). A senior engineer
about to ship asks what the recipient will do differently, by when, and whether they would
learn it anyway; what each kind of error costs, and whether the step can be undone. They make
the best case for not acting, or for the best alternative, and act when the case for acting
still stands.

**Being challenged** ("are you sure?"). An expert witness neither defends nor folds: restate
the claim, name the premise most likely to be wrong, observe it, and answer with what you
observed and whether the claim changed. Changing the answer only because the question was
asked again is not a check.
