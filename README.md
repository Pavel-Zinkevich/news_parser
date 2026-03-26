lemonde_today.py

A small script that fetches Le Monde's live page and prints all article headings and links published today, indicating if they are subscriber-only.

Usage

1. (Optional) create a virtualenv:

   python3 -m venv .venv
   source .venv/bin/activate

2. Install requirements:

   python3 -m pip install -r 'requirements.txt'

3. Run the script:

   python3 'lemonde_today.py'

If the site returns a 402 (Payment Required) you can provide your browser cookies so the script can fetch the page:

   # copy the Cookie header from your browser's developer tools network request and then:
   export LEMONDE_COOKIE='name=value; name2=value2'
   python3 lemonde_today.py

Or pass it directly:

   python3 lemonde_today.py --cookie 'name=value; name2=value2'

Notes and assumptions

- The script looks for either the string "Publié aujourd'hui" (or the curly apostrophe variant) near a link, or a date segment in the article URL matching today's date (YYYY/MM/DD).
- It marks articles as subscriber-only when it finds phrases like "Article réservé" or similar near the title.
- The site layout can change; if the script returns no articles, try inspecting the page HTML or adjusting heuristics in `lemonde_today.py`.

License: MIT (use for personal or educational purposes)
