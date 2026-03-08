from pathlib import Path
import re, sys
try:
    text = Path('.streamlit/secrets.toml').read_text()
except Exception as e:
    print('ERROR: cannot read .streamlit/secrets.toml:', e); sys.exit(1)
m = re.search(r'GEMINI_API_KEY\s*=\s*"([^"]+)"', text)
if not m:
    print('ERROR: GEMINI_API_KEY not found in .streamlit/secrets.toml'); sys.exit(1)
key = m.group(1)
import google.generativeai as genai
try:
    genai.configure(api_key=key)
    model = genai.GenerativeModel('gemini-3.1-flash-lite-preview')
    cfg = genai.GenerationConfig(max_output_tokens=40, temperature=0.1)
    resp = model.generate_content('Auf Deutsch: Nenne in einem Satz "Hallo Welt" mit Emoji.', generation_config=cfg)
    print('API_OK')
    print(resp.text)
except Exception as e:
    print('API_ERROR', type(e).__name__, e); sys.exit(2)
