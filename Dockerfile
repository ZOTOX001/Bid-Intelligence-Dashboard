FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	STREAMLIT_SERVER_HEADLESS=true \
	STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
	STREAMLIT_SERVER_PORT=8501

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
	&& pip install --no-cache-dir -r requirements.txt

COPY app.py gem_master_bids.csv ./
COPY input_pdfs/ ./input_pdfs/
COPY downloaded_attachments/ ./downloaded_attachments/

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
	&& chown -R appuser:appuser /app
USER appuser

EXPOSE 8501

CMD ["streamlit", "run", "app.py"]
