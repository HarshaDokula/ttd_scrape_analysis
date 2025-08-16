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