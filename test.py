import streamlit as st
import pytesseract
from PIL import Image

st.title("KI-Lernhelfer: Buchseite digitalisieren")
file = st.file_uploader("Lade ein Foto deiner Buchseite/Notiz hoch", type=["png", "jpg", "jpeg"])

if file is not None:
    # Bild anzeigen
    st.image(file, caption="Hochgeladenes Bild", use_container_width=True)
    
    # OCR durchführen
    image = Image.open(file)
    text = pytesseract.image_to_string(image, lang="deu")
    
    # Text anzeigen
    st.subheader("Erkannter Text:")
    st.write(text)
    
    # Button mit Placeholder
    if st.button("Text didaktisch aufbereiten"):
        st.info("Hier wird später die KI-Logik für die Aufbereitung eingebaut.")