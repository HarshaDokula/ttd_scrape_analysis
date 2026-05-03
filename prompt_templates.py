ttd_prompt_tmpl = '''
You are a classifier for news articles from the Tirumala Tirupati Devasthanams (TTD).
### TASK
Given an article title and article text, return true or false following these rules:

### RULES
- true: Daily/periodic pilgrim statistics at Tirumala, e.g., "About 64,801 pilgrims had Srivari darshan..."
- false: All other news (festivals, dignitaries visits, events, admin notices, etc.).

Title: {title}
Article text: {article_text}
'''


ttd_prompt_tmpl2 = '''
You are a strict classifier for news articles from the Tirumala Tirupati Devasthanams (TTD).

### TASK
Return ONLY "true" or "false" - nothing else.

### RULES
- "true": ONLY if the Title or Article text contains ACTUAL DAILY VISITOR COUNT NUMBERS (e.g., "About 64,801 pilgrims...", "52,643 pilgrims")
- "false": Everything else INCLUDING:
  - Ticket releases, booking announcements, online DIP
  - Festival/event descriptions, maintenance notices
  - Administrative announcements, dignitaries visits
  - General news about arrangements or facilities
  - Articles mentioning pilgrims but NO actual count numbers

CRITICAL: Look for ACTUAL NUMBERS. Arrangement/logistics articles should be "false" even if they mention pilgrims or tickets.

### EXAMPLES
Title: About 23,423 pilgrims had Srivari Darshan from 3am to 6pm on October 31
Answer: true

Title: TTD ALLOTS SEVA TICKETS THROUGH ONLINE DIP
Answer: false

Title: UNPRECEDENTED RUSH CONTINUES IN TIRUMALA
Answer: false

### QUESTION
Title: {title}
Article text: {article_text}

answer:
'''

ttd_info_extract_prompt_tmpl = '''
You are an information extractor for news articles from the Tirumala Tirupati Devasthanams (TTD).

Your task is to extract the following information from the article text following these rules:
- Day of the article. If the day is not present in the article, use null. For example, if only "September" is provided, output {{"day":null}}; if the text contains "September 9", output {{"day":9}}.
- Number of pilgrims visiting Tirumala in the article text
- Any other relevant metrics mentioned in the article
- If the article does not contain any of the above information, return null for each field.
- DO not deviate from the rules or create incorrect information.

### OUTPUT FORMAT
Return a JSON object with the following fields:
```json
{{
    
    "day": <digit day from the article content or null if not present>,
    "pilgrim_count": <number>,
    "other_metrics": <any other relevant metrics>
}}
```

### INPUT
Aritcle text: {article_text}

Here's the json object with the extracted information:
```json'''