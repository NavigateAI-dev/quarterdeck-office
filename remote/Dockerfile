# The office's private remote route (MA3): Tailscale, in its own container,
# sharing the office container's network so Serve can reach 127.0.0.1:8000.
# Serve only, inside the owner's tailnet; never Funnel, never a port on the Mac.
FROM tailscale/tailscale:v1.102.5@sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065
RUN apk add --no-cache python3
COPY remote/control.py /control.py
ENTRYPOINT ["python3", "-u", "/control.py"]
