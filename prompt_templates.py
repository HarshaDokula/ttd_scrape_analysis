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