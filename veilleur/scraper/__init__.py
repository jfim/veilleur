"""Webpage scraping.

Fetches pages via passe-partout's stateful tab flow (waiting for network
idle), parses the HTML with lxml, and extracts items using a feed's stored
xpath. Scrapes are strictly serial (no concurrency) per the project spec.
"""
