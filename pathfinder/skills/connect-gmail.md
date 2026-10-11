---
name: connect-gmail
description: Use when the owner asks to connect Gmail or read their mail.
---
# Connect Gmail

PATHFINDER reads and searches Gmail through its mail sense, over IMAP with an app password. It
never sends mail. If the mail sense already answers (`list_mail`), skip this and use `list_mail`,
`search_mail` and `read_mail`. Otherwise walk the owner through the seven steps below, one at a
time; three of them are on Google's pages. Never ask for the app password in the chat.

1. **The check.** Signed in to the Gmail account, the owner opens
   `myaccount.google.com/apppasswords`.
   - If the page offers to create an app password, go on.
   - If it says the setting is not available for the account: app passwords need 2-Step
     Verification on (step 2). A Workspace administrator can also switch off 2-Step
     Verification, app passwords or IMAP itself; then see the last paragraph.
2. **2-Step Verification on**, if it is not.
3. **Create an app password** named PATHFINDER, and keep its 16 characters on screen.
4. **Turn the mail sense on** with
   `pathfinder config set senses.mail '{"host": "imap.gmail.com", "user": "<your address>"}'`.
   It changes the mail sense only; the other senses stay on. Never run `config set senses`
   with a whole object: it replaces every sense.
5. **`/secret mail`** in the screen (`pathfinder secret mail` in a shell), then paste the app
   password. The input is masked and goes to the Keychain; it never passes through the chat. If
   the screen asks for the app password by itself, that prompt is the same intake.
6. **`pathfinder service restart`**: senses load at start. The owner runs it in a shell after
   this turn; do not run it yourself, it ends the turn without an answer.
7. **The first poll.** Ask "What's in my inbox?" A login refusal here means IMAP is off for the
   account.

If the account refuses an app password, the fallback is a local Gmail server with its own OAuth
(about ten steps in Google's console, consent repeated weekly); it is not set up yet, so say so
and stop.
