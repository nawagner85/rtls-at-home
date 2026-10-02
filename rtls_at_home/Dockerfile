# The RTLS@Home App a household installs (tools/assemble_app.py puts the engine's source in engine/). Home Assistant
# builds it on its own machine (aarch64 on a Raspberry Pi, amd64 elsewhere). The base is Debian's Python, not Home
# Assistant's Alpine base: numpy, scipy and opencv ship wheels for it on both.
FROM docker.io/library/python:3.12-slim
RUN pip install --no-cache-dir numpy==1.26.4 scipy==1.15.3 opencv-python-headless==4.10.0.84 pillow==10.4.0
WORKDIR /opt/rtls
COPY engine/solver/ solver/
COPY engine/app/rtls/ app/rtls/
COPY run.sh /run.sh
# The model: the developer's image's (app/Dockerfile), without that installation's survey, registry and Bermuda
# reference (tests/test_assemble_app.py holds them equal).
ENV RP_AZGAIN=1
ENV RP_WAPUP=1
ENV RTLS_RECEIVER_LEVELS=on RTLS_RECEIVER_TAU=60
# The App: the bridge feeds it; its house, data and ingest token live in /data; an import waits in its config folder.
ENV PYTHONUNBUFFERED=1 RTLS_SOURCE=ingest RTLS_SESSIONS=/data RTLS_INGEST_TOKEN_FILE=/data/ingest_token
ENV RTLS_IMPORT_DIR=/config/import RTLS_PORT=8765
RUN chmod a+x /run.sh
CMD ["/run.sh"]
