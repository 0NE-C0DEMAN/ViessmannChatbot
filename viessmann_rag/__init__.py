"""Viessmann RAG Chatbot — retrieval-augmented chat over technical PDFs.

Public entry points:
  - viessmann_rag.ingest.cli.main()      → ingest CLI
  - viessmann_rag.chat.server.run()      → Flask app on CHAT_PORT
  - viessmann_rag.chat.server.create_app()  → Flask app factory (for tests)
  - viessmann_rag.retrieval.retrieve(question)  → ranked chunks

See README.md for setup, configuration, and architecture notes.
"""

__version__ = "1.3.0"

