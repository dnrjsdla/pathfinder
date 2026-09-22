---
quiet_hours: 23-8
daily_budget: 8
min_interval_s: 30
burst_limit: 5
judgment_cap: 20
idle_threshold_s: 20
---

# RULES

The code parses this file and turns every signal into one of `speak · silence · defer ·
ask_model`. Only `ask_model` goes to a model turn - everything else is free.

Three things to know:

- The frontmatter above is the **global gate**. `quiet_hours` and `daily_budget` apply to every
  kind first; hitting either is `defer` (not silence - the signal is seen again once the window
  passes or the day turns over). A signal the user themselves started (a schedule they wrote) is
  exempt from the budget, never from quiet hours.
- Each `## <kind>` table below is that kind's rules. **First match, top to bottom, wins.** No
  match at all -> `ask_model`.
- A value of `USER.xxx` reads that list from `USER.md`'s frontmatter. No names or addresses live
  in this file - that is `USER.md`'s job (empty for now; partition 5 fills it in).

The condition words are closed: `always`, `count_over`, `minutes_until_under`, `minutes_over`,
plus whatever each loaded sense registers. Enlarging the vocabulary means changing the code that
reads this file - that is the point: an unknown word here is a mistake, not a new feature.

## schedule_due

| condition | value | action | note |
| --- | --- | --- | --- |
| `always` | `-` | `speak` | the user's own reminder - always say it |

## command_failed

| condition | value | action | note |
| --- | --- | --- | --- |
| `count_over` | `3` | `speak` | three or more failures today - something is stuck |
| `always` | `-` | `silence` | one failure alone is ordinary noise |

## command_finished

| condition | value | action | note |
| --- | --- | --- | --- |
| `minutes_over` | `5` | `speak` | a command that ran more than five minutes is worth a word when it's done |
| `always` | `-` | `silence` | short commands finish silently |

<!--
The senses below do not exist yet (they land in the next build step); their kinds are commented
out so an unloaded sense's words are never checked against the closed vocabulary.

## new_mail

| condition | value | action | note |
| --- | --- | --- | --- |
| `sender_in` | `USER.waiting_for` | `speak` | someone you're waiting on |
| `sender_domain_in` | `USER.ignore_domains` | `silence` | newsletters, notifications |
| `always` | `-` | `ask_model` | an unknown sender goes to the model |

## upcoming_event

| condition | value | action | note |
| --- | --- | --- | --- |
| `all_day` | `true` | `silence` | an all-day entry is not something to act on now |
| `minutes_until_under` | `30` | `speak` | thirty minutes out is worth a word |
| `always` | `-` | `ask_model` | |

## due_reminder

| condition | value | action | note |
| --- | --- | --- | --- |
| `minutes_until_under` | `60` | `speak` | a deadline within the hour |
| `always` | `-` | `ask_model` | |

## overdue_reminder

| condition | value | action | note |
| --- | --- | --- | --- |
| `always` | `-` | `silence` | a reminder already missed is not an interruption |

## task_done

| condition | value | action | note |
| --- | --- | --- | --- |
| `always` | `-` | `silence` | speaks only when presence says the user is away (code, not a rule) |

## needs_input

| condition | value | action | note |
| --- | --- | --- | --- |
| `always` | `-` | `silence` | speaks only when presence says the user is away (code, not a rule) |
-->
