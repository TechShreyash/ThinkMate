# Requirements Document

## Introduction

This feature enhances ThinkMate, a Telegram bot that participates in group chats and direct
messages (DMs), in two related areas of "feeling more like a real person."

**Part A — Implicit (natural) bot-addressing in group chats.** Today the bot only treats a
group message as directed at it when the message is *explicitly* addressed: an @mention of the
bot's username, the bot's name used as a word, or a Telegram reply to one of the bot's own
messages. Every other message falls through to a probabilistic "ambient gate" that only
occasionally chimes in. In real conversations people frequently answer the bot without using the
reply button or its name — for example, the very next message after the bot speaks is often a
direct response to it. The bot should recognize these implicitly-addressed messages (especially
right after it has spoken) and reply directly, while staying conversational rather than noisy and
without changing DM behavior.

**Part B — Memory language normalization and reply language/script matching.** All memory the
bot stores about a user (facts, beliefs, events) must be stored in English regardless of the
language the user speaks. Separately, the bot's *replies* should match the language and script
the user is currently using — including the Hindi case where a user may write in Devanagari Hindi
or in Hinglish (Hindi written with Latin/English letters), and the reply should follow whichever
the user is using.

These two parts are independent in implementation but share the goal of making the bot's
conversational behavior feel natural. Existing invariants — the single-write buffer rule, the
per-chat ambient cooldown, affinity signals, and unchanged DM routing — must be preserved.

## Glossary

- **Bot**: The ThinkMate Telegram bot process that reads messages and produces replies.
- **Group_Router**: The routing logic that classifies an incoming group/supergroup message and
  decides whether the Bot replies directly, runs the ambient gate, or stays silent
  (`app/handlers/messages.py`, `_handle_group_message`).
- **Explicit_Address**: A group message that targets the Bot via an @mention of the Bot's
  username, the Bot's name used as a standalone word, or a Telegram reply to one of the Bot's
  own messages (the existing `is_addressed` definition).
- **Implicit_Address_Detector**: The new no-LLM decision component that determines whether a
  non-explicitly-addressed group message is very likely intended for the Bot, based on
  conversational context such as how recently the Bot last spoke.
- **Implicit_Address**: A group message that is not an Explicit_Address but is judged by the
  Implicit_Address_Detector to be very likely directed at the Bot.
- **Ambient_Gate**: The existing pure, no-LLM probabilistic funnel (cooldown → trigger/scan-tick
  → affinity-weighted dice roll) that decides whether to volunteer a chime-in on a message that
  is neither an Explicit_Address nor an Implicit_Address (`AmbientGate` in
  `app/services/group_gate.py`).
- **Bot_Recency_Window**: A configurable span — measured in elapsed time and/or number of
  intervening human messages — immediately following the Bot's most recent message in a group,
  during which a follow-up message is a candidate for Implicit_Address.
- **Directed_At_Other**: A message that is clearly aimed at a specific participant other than the
  Bot (for example, it @mentions another user or replies to another user's message).
- **Implicit_Cooldown**: A configurable per-chat throttle that limits how frequently the Bot will
  treat implicitly-addressed follow-ups as directed replies, to avoid the Bot dominating the chat.
- **Affinity**: The existing per-(chat, user) score in `[0, 1]` measuring how welcome the Bot is
  to a given person, stored in `chat_members` and managed by `Affinity_Cache`.
- **Affinity_Cache**: The existing write-through cache for Affinity and mode
  (`app/services/affinity.py`).
- **Single_Write_Invariant**: The rule that every group message is recorded to the buffer exactly
  once — never zero times, never twice.
- **Memory_Extractor**: The background component that extracts facts/beliefs/events from buffered
  conversation into per-user profiles (`app/services/memory_extractor.py`).
- **Stored_Memory**: Any fact, belief, or event the Bot persists about a user.
- **Reply_Generator**: The component that produces the Bot's conversational reply text, driven by
  the system prompt (`app/prompts/system_prompt.py`).
- **User_Language**: The natural language the user is currently writing in.
- **User_Script**: The writing system the user is currently using (for example, Devanagari or
  Latin script).
- **Hinglish**: Hindi expressed using Latin/English letters.
- **Devanagari_Hindi**: Hindi expressed using the Devanagari script.
- **DM**: A private one-on-one chat where `chat_id == user_id`.

## Requirements

### Requirement 1: Detect implicitly-addressed group messages

**User Story:** As a member of a group chat, I want the Bot to understand when I am answering it
without tagging it, so that the conversation feels natural instead of forcing me to use the reply
button or its name.

#### Acceptance Criteria

1. WHEN a group message is received that is not an Explicit_Address, THE Group_Router SHALL invoke
   the Implicit_Address_Detector before invoking the Ambient_Gate.
2. WHEN the Implicit_Address_Detector evaluates a message AND the Bot's most recent message in that
   chat falls within the Bot_Recency_Window, THE Implicit_Address_Detector SHALL classify the
   message as an Implicit_Address, except where Requirement 2 applies.
3. WHEN the Implicit_Address_Detector evaluates a message AND the Bot's most recent message in that
   chat falls outside the Bot_Recency_Window, THE Implicit_Address_Detector SHALL classify the
   message as not an Implicit_Address.
4. WHERE the Bot has never spoken in a group chat, THE Implicit_Address_Detector SHALL classify
   messages in that chat as not an Implicit_Address.
5. THE Implicit_Address_Detector SHALL reach a decision using only in-memory state and the message
   content without making a large language model call.
6. IF evaluating the Implicit_Address_Detector raises an error, THEN THE Group_Router SHALL treat
   the message as not an Implicit_Address and continue to the Ambient_Gate.

### Requirement 2: Avoid hijacking messages meant for other people

**User Story:** As a group participant talking to someone other than the Bot, I want the Bot to
stay out of the conversation, so that it does not interrupt exchanges that are not about it.

#### Acceptance Criteria

1. WHEN a non-explicitly-addressed message is a Directed_At_Other message, THE
   Implicit_Address_Detector SHALL classify the message as not an Implicit_Address.
2. WHEN a message replies to another participant's message, THE Implicit_Address_Detector SHALL
   classify the message as Directed_At_Other.
3. WHEN a message @mentions a participant other than the Bot AND does not also address the Bot,
   THE Implicit_Address_Detector SHALL classify the message as Directed_At_Other.

### Requirement 3: Reply directly to implicitly-addressed messages

**User Story:** As a group member, I want the Bot to reply directly when I am clearly talking to
it, so that I get an answer instead of silence or a random unrelated chime-in.

#### Acceptance Criteria

1. WHEN a group message is classified as an Implicit_Address AND the Implicit_Cooldown has elapsed
   for that chat, THE Group_Router SHALL enqueue the message as a direct reply rather than handing
   it to the Ambient_Gate.
2. WHEN the Group_Router enqueues an Implicit_Address as a direct reply, THE Group_Router SHALL
   record the implicit-reply decision in the logs with the chat identifier.
3. WHEN the Group_Router enqueues an Implicit_Address as a direct reply, THE Group_Router SHALL
   reset the Implicit_Cooldown for that chat before enqueueing.
4. WHEN the Group_Router enqueues an Explicit_Address or an Implicit_Address as a direct reply,
   THE Group_Router SHALL preserve the Single_Write_Invariant by not writing the message to the
   buffer itself.

### Requirement 4: Throttle implicit replies to avoid noise

**User Story:** As a group member, I want the Bot to avoid jumping on every follow-up message, so
that it stays pleasant and does not flood the chat.

#### Acceptance Criteria

1. WHEN a message is classified as an Implicit_Address AND the Implicit_Cooldown has not elapsed
   for that chat, THE Group_Router SHALL hand the message to the Ambient_Gate instead of replying
   directly.
2. THE Implicit_Cooldown SHALL be configurable through an application configuration value.
3. THE Bot_Recency_Window SHALL be configurable through application configuration values for its
   elapsed-time bound and its intervening-message-count bound.
4. WHEN more than one participant sends an Implicit_Address within a single Implicit_Cooldown
   window, THE Group_Router SHALL reply directly to at most one of those messages and hand the
   remaining ones to the Ambient_Gate.

### Requirement 5: Preserve existing routing and DM behavior

**User Story:** As a user, I want existing direct-message and group behaviors to keep working, so
that this enhancement does not regress what already works.

#### Acceptance Criteria

1. WHEN a message arrives in a DM, THE Group_Router SHALL apply the existing private-chat path
   without invoking the Implicit_Address_Detector.
2. WHEN a group message is an Explicit_Address, THE Group_Router SHALL reply directly using the
   existing addressed path without invoking the Implicit_Address_Detector.
3. WHEN a group message is neither an Explicit_Address nor an Implicit_Address that passes the
   Implicit_Cooldown, THE Group_Router SHALL hand the message to the Ambient_Gate as it does today.
4. WHEN a message is handed to the Ambient_Gate because it was not treated as a direct reply, THE
   Group_Router SHALL preserve the Single_Write_Invariant by ensuring the message is recorded to
   the buffer exactly once.
5. WHILE the Bot processes a group message, THE Group_Router SHALL continue to apply the existing
   Affinity signals for engagement and back-off.

### Requirement 6: Track the Bot's recent activity per chat

**User Story:** As a developer, I want the Bot to know when it last spoke in each group, so that
the Implicit_Address_Detector has the context it needs to judge follow-ups.

#### Acceptance Criteria

1. WHEN the Bot sends a message in a group chat, THE Bot SHALL record the time of that message for
   that chat.
2. WHEN a human message is received in a group chat, THE Bot SHALL update the count of human
   messages observed since the Bot's most recent message in that chat.
3. THE Bot SHALL store the recent-activity tracking state in memory keyed by chat identifier.
4. WHEN recent-activity tracking state for a chat has been idle beyond a bounded horizon, THE Bot
   SHALL remove that chat's tracking state to keep memory bounded.

### Requirement 7: Store all user memory in English

**User Story:** As a maintainer, I want every stored memory to be in English, so that profiles are
consistent and queryable regardless of the language the user speaks.

#### Acceptance Criteria

1. WHEN the Memory_Extractor extracts a fact, belief, or event from a conversation in a language
   other than English, THE Memory_Extractor SHALL store the resulting Stored_Memory in English.
2. WHEN the Memory_Extractor extracts memory from an English conversation, THE Memory_Extractor
   SHALL store the resulting Stored_Memory in English.
3. THE Memory_Extractor SHALL store proper nouns, names, and quoted identifiers in their original
   form within an English Stored_Memory.
4. WHEN the Memory_Extractor processes a multi-party group segment, THE Memory_Extractor SHALL
   store each participant's Stored_Memory in English.

### Requirement 8: Match the user's language and script in replies

**User Story:** As a user who speaks Hindi, I want the Bot to reply in the same language and script
I am using, so that talking to it feels comfortable and natural.

#### Acceptance Criteria

1. WHEN the user writes in a given User_Language, THE Reply_Generator SHALL produce the reply in
   that same User_Language.
2. WHEN the user writes in Hinglish, THE Reply_Generator SHALL produce the reply in Hinglish.
3. WHEN the user writes in Devanagari_Hindi, THE Reply_Generator SHALL produce the reply in
   Devanagari_Hindi.
4. WHEN the user changes the User_Language or User_Script during a conversation, THE
   Reply_Generator SHALL produce subsequent replies in the User_Language and User_Script indicated
   by the recent conversation context rather than reacting to a single isolated message.
5. THE Reply_Generator SHALL match the User_Language and User_Script for replies independently of
   the requirement to store Stored_Memory in English.

### Requirement 9: Do not respond to mass-tagging / spam patterns

**User Story:** As a group member, I want the Bot to ignore automated mass-tagging spam (for
example a userbot that tags every member with "hi" or "good morning"), so that the Bot does not
waste resources replying to bulk noise or amplify the spam.

#### Glossary additions

- **Mass_Tag_Spam**: A group message exhibiting bulk-tagging or automated-greeting characteristics
  — for example, it @mentions many participants at once, and/or it is a short low-content greeting
  broadcast to the group (such as "hi", "good morning") rather than a genuine message directed at
  the Bot.

#### Acceptance Criteria

1. WHEN a group message contains @mentions of more than a configurable threshold of distinct
   participants, THE Implicit_Address_Detector SHALL classify the message as Mass_Tag_Spam.
2. WHEN a group message is classified as Mass_Tag_Spam, THE Implicit_Address_Detector SHALL
   classify the message as not an Implicit_Address even if the Bot spoke within the
   Bot_Recency_Window.
3. WHEN a group message is classified as Mass_Tag_Spam, THE Ambient_Gate SHALL NOT treat the
   message's cheap-trigger keywords (for example greetings) as an ambient trigger.
4. WHEN a group message is classified as Mass_Tag_Spam AND the only signal addressing the Bot is
   an @mention of the Bot's own username included within the bulk mention list, THE Group_Router
   SHALL NOT treat the message as an Explicit_Address.
5. WHERE a Mass_Tag_Spam message replies to one of the Bot's own messages (a deliberate
   reply-to-bot signal that automated mass-tagging does not produce), THE Group_Router SHALL still
   treat the message as an Explicit_Address and reply.
6. IF evaluating the Mass_Tag_Spam classification raises an error, THEN THE Bot SHALL treat the
   message as not Mass_Tag_Spam and continue normal processing.
7. THE threshold of distinct @mentions that marks a message as Mass_Tag_Spam SHALL be configurable
   through an application configuration value.

### Requirement 10: Suppress repetitive greeting-burst spam over time

**User Story:** As a group member, I want the Bot to ignore automated greeting-burst spam — for
example a userbot that tags participants one-by-one in rapid succession with near-identical
messages such as "hi" or "good morning" — so that the Bot does not waste large language model
resources replying to bulk noise or amplify the spam.

#### Glossary additions

- **Greeting_Burst_Spam**: A pattern of repetitive, low-content greeting messages (such as "hi" or
  "good morning") received in a single chat in rapid succession within a short time window, where
  the messages are near-identical in their non-mention content and each typically @mentions a
  different single participant — characteristic of an automated userbot greeting the group one
  member at a time. Greeting_Burst_Spam is distinct from Mass_Tag_Spam (Requirement 9), which
  concerns a single message that @mentions many participants at once.
- **Spam_Burst_Detector**: The new no-LLM, stateful decision component that tracks recent per-chat
  message content and arrival times in memory to identify Greeting_Burst_Spam without making a
  large language model call.

#### Acceptance Criteria

1. WHEN the Spam_Burst_Detector compares two group messages, THE Spam_Burst_Detector SHALL exclude
   @mention tokens from each message before computing similarity, so that greeting messages tagging
   different participants are compared on their remaining content.
2. WHEN the Spam_Burst_Detector computes the similarity of two mention-excluded message contents AND
   that similarity meets or exceeds a configurable similarity threshold, THE Spam_Burst_Detector
   SHALL treat the two messages as near-identical.
3. WHEN the count of near-identical greeting messages received in a single chat within a
   configurable time window reaches a configurable burst-count threshold, THE Spam_Burst_Detector
   SHALL classify those messages as Greeting_Burst_Spam.
4. WHEN a group message is classified as Greeting_Burst_Spam, THE Implicit_Address_Detector SHALL
   classify the message as not an Implicit_Address even if the Bot spoke within the
   Bot_Recency_Window.
5. WHEN a group message is classified as Greeting_Burst_Spam, THE Ambient_Gate SHALL NOT treat the
   message's cheap-trigger keywords as an ambient trigger.
6. WHEN a group message is classified as Greeting_Burst_Spam AND the only signal addressing the Bot
   is an @mention of the Bot's own username, THE Group_Router SHALL NOT treat the message as an
   Explicit_Address.
7. WHERE a Greeting_Burst_Spam message replies to one of the Bot's own messages, THE Group_Router
   SHALL treat the message as an Explicit_Address and reply.
8. WHEN a single greeting message is received that is not part of a Greeting_Burst_Spam pattern, THE
   Spam_Burst_Detector SHALL classify the message as not Greeting_Burst_Spam.
9. WHEN a group message is an Explicit_Address that is not classified as Greeting_Burst_Spam, THE
   Group_Router SHALL treat the message as an Explicit_Address using the existing addressed path.
10. THE Spam_Burst_Detector SHALL reach each classification using only in-memory state and message
    content without making a large language model call.
11. THE similarity threshold, THE burst-count threshold, and THE time window SHALL each be
    configurable through an application configuration value.
12. WHERE a configuration value among the similarity threshold, the burst-count threshold, and the
    time window is not explicitly set, THE Bot SHALL apply a default value for that configuration
    value.
13. WHEN the Spam_Burst_Detector's per-chat tracking state has been idle beyond a bounded horizon,
    THE Spam_Burst_Detector SHALL remove that chat's tracking state to keep memory bounded.
14. IF evaluating the Greeting_Burst_Spam classification raises an error, THEN THE Group_Router
    SHALL treat the message as not Greeting_Burst_Spam and continue normal processing.
