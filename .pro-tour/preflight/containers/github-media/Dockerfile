FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
RUN apt-get update && apt-get install -y --no-install-recommends socat ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY gh /opt/gh
COPY pro_tour /opt/media/pro_tour
COPY runtime.py gateway.py egress_proxy.py /opt/media/
COPY body.md /opt/fixtures/body.md
RUN ffmpeg -v error -f lavfi -i testsrc2=duration=4:size=640x360:rate=24 \
    -c:v libx264 -pix_fmt yuv420p -movflags +faststart -an /opt/fixtures/app.mp4 \
    && ffmpeg -v error -f lavfi -i testsrc2=duration=4:size=640x360:rate=24 \
    -vf hue=h=120 -c:v libx264 -pix_fmt yuv420p -movflags +faststart -an /opt/fixtures/feature.mp4 \
    && install -d -o 10000 -g 10000 /run/pro-tour/media /workspace
USER 10000:10000
WORKDIR /workspace
ENTRYPOINT ["python", "/opt/media/runtime.py"]
