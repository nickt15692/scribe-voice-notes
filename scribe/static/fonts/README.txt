Archivo — bundled webfont
=========================

Files
  archivo-latin.woff2        weights 400-700 (variable), Latin
  archivo-latin-ext.woff2    weights 400-700 (variable), Latin Extended
  archivo-vietnamese.woff2   weights 400-700 (variable), Vietnamese

Each file is a variable font covering the whole 400-700 range, which is why
scribe/static/index.html declares `font-weight: 400 700` once per subset rather
than one @font-face per weight. The unicode-range values there are Google's own,
so a browser still fetches only the subsets a page actually needs.

Source
  Downloaded from fonts.gstatic.com (Google Fonts, Archivo v25) and vendored so
  the app makes no third-party requests. Previously index.html linked
  fonts.googleapis.com, which disclosed the viewer's IP address to Google on
  every page load. It never carried audio or transcript content.

To update
  curl -A "<a modern browser UA>" \
    "https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&display=swap"
  then download the .woff2 URLs it returns and replace these files. A modern
  User-Agent matters: Google serves older formats to unrecognised clients.

License
  Archivo is (c) The Archivo Project Authors (https://github.com/Omnibus-Type/Archivo)
  and is licensed under the SIL Open Font License, Version 1.1.
  Full text: https://openfontlicense.org/

  The OFL permits bundling and redistribution with the software. The font files
  are unmodified. They must not be sold on their own, and any derivative font
  must not use the reserved name "Archivo".
