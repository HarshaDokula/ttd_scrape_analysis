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
You are a classifier for news articles from the Tirumala Tirupati Devasthanams (TTD).

### TASK
Given an article title and article text, return true or false following these rules:

### RULES
- true: If the Title or Article text contains information on count of pilgrims visiting Tirumala, e.g., "About 64,801 pilgrims.."
- false: Anything other than information on people count.

### EXAMPLES
Title: About 23,423 pilgrims had Srivari Darshan from 3am to 6pm on October 31
Article text: About 23,423 pilgrims had Srivari Darshan from 3am to 6pm on October 31. Current Situation of Pilgrim darshan particulars of various categories of darshan line with waiting compartments and waiting hours details-Sarva Darshan (Free darshan)- 6 compartments/4 hours; Divya Darshan (Footpath darshan)- 3 compartments/2 hours; Special Entry Darshan (Rs.300 Darshan) closed.

answer: true

Title: SRI B.VENKATESWARA RAO SWORN IN AS TTD BOARD EX-OFFICIO
Article text: TIRUMALA, Oct 31: The 1993-Batch IAS Officer, Sri Busani.Venkateswara Rao, the Secretary to Revenue Endowment, Govt. of AP, sworn in as the ex-officio member of TTD trust board on Thursday. The TTD Executive officer Sri M.G.Gopal administered the oath of office to Sri Venkateswara Rao at the Bangaru Vakili of Tirumala temple on Thursday night at.

answer: false

### QUESTION
Title: {title}
Article text: {article_text}

answer:
'''

# ttd_info_extract_prompt_tmpl = '''
# You are an information extractor for news articles from the Tirumala Tirupati Devasthanams (TTD).

# Your task is to extract the following information from the article text following these rules:
# - A JSON Date of the article. For any missing parts of the date (year, month, or day), use 0. For example, if only "September 31" is provided, output {{"date":{{"year":null,"month":9,"day":31}}}}; if all parts are present, use them.
# - Number of pilgrims visiting Tirumala in the article text
# - Any other relevant metrics mentioned in the article
# - If the article does not contain any of the above information, return null for each field.
# - DO not deviate from the rules or create incorrect information.

# ### OUTPUT FORMAT
# Return a JSON object with the following fields:
# ```json
# {{
#     "date":{{
#         "year": <digit year from the article content or 0 if not present>,
#         "month": <digit month from the article or 0 if not present>,
#         "day": <digit day from the article content or 0 if not present>
#     }},
#     "pilgrim_count": <number>,
#     "other_metrics": <any other relevant metrics>
# }}
# ```

# ### INPUT
# Aritcle text: {article_text}

# Here's the json object with the extracted information:
# '''

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
```json

'''