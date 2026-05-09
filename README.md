# Veilleur

Monitors webpages and turns them into RSS/Atom feeds

Main features:
- Web UI to add webpages to turn into RSS feeds, alongside with polling frequency
- RSS feeds are served by veilleur, eg. /feeds/{id}/rss or /feeds/{id}/atom
- REST API to programmatically add new webpages to turn into RSS feeds and manage them
- REST API to fetch historical items that aren't present in the latest scrape, eg. /feeds/{id}/items
- Saves all of extracted items into a postgres database
- RSS feeds are extracted through XPath expressions (see in directory ~/projects/rss-ify/ the files dump_anchors.py, derive_xpath.py, prompt.txt)
- XPath expressions are built automatically using a LLM, as shown above
- When scanning, the list of new links should be compared with the ones in the previous batch:
  - If nothing matches, the page might have changed in ways that are incompatible, try regenerating a new xpath expression, and if that doesn't work, mark it as failed
  - New links should be evaluated to see if they match the longest common prefix in the previous run, excluding path parts that have numbers
    eg. if the previous run had example.com/posts/2026/mypost.html and example.com/posts/2026/another.html, the prefix used for comparison is example.com/posts/ (excluding the numeric part)
	This ensures that if the xpath expression matches new URL schemes like say example.com/something.html we can detect the xpath failure to work correctly
	If the new links don't match, regenerate an xpath expression, success if all links match previous ones or new links also match the correct prefix, fail if we get no matches or links that don't match the previous prefix

Dev setup:
- Use uv for package management (eg `uv init`)
- Use just for launching the various commands in the project
- Use ruff and pyright
- Set up github CI with proper caching
- Set up CLAUDE.md as necessary
