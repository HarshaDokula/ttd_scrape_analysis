ttd_prompt_tmpl3 = '''
You are a strict classifier for Tirumala Tirupati Devasthanams (TTD) news articles.

### TASK
Return ONLY the token "true" or "false" (lowercase) and nothing else.

### RULES
- "true": Only if the article title or text contains an ACTUAL DAILY PILGRIM COUNT (e.g., "About 64,801 pilgrims", "52,643 pilgrims at Srivari darshan").
- "false": Everything else, including:
  - Ticket releases, booking announcements, online DIP, or administrative logistics
  - Festival/event descriptions, maintenance notices, or dignitary visits
  - General news about arrangements, facilities, or pilgrim guidance without concrete counts
  - Articles mentioning pilgrims without an explicit count number

CRITICAL: Focus on the first {token_budget} tokens of the article text. Disallow any explanation or extra context beyond "true"/"false".

Title: {title}
Article text: {article_text}
Answer:
'''

ttd_info_extract_prompt_tmpl_v2 = '''
You are a precise JSON extractor for Tirumala Tirupati Devasthanams (TTD) pilgrim reports.

### TASK
Return a single JSON object with keys "day", "pilgrim_count", and "other_metrics".
- day: numeric day extracted from the article text (1-31) or null if missing.
- pilgrim_count: integer count of pilgrims noted in the article.
- other_metrics: a dictionary of any additional numeric or descriptive metrics found (use empty object {{}} if none).

### RULES
- Output nothing but the JSON object (no prose, no markdown).
- Strip any enclosing triple backticks or stop tokens before parsing the JSON.
- If a field is missing, use null (day), 0 (pilgrim_count), or {{}} (other_metrics).

### INPUT
Article text (first {token_budget} tokens): {article_text}

Return the JSON object now.
'''
