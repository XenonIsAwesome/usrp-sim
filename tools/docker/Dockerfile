# usrp-sim container.  Build context = tools/usrp_sim (use tools/docker_build.sh).
FROM python:3.12-slim
RUN pip install --no-cache-dir numpy msgpack \
 && useradd --system --create-home --home-dir /var/lib/usrp-sim usrp-sim \
 && mkdir -p /config && chown usrp-sim /config
WORKDIR /opt/usrp-sim
COPY usrp_sim/ ./usrp_sim/
COPY examples/ ./examples/
COPY README.md ./
COPY tools/docker/entrypoint.sh /usr/local/bin/usrp-sim-entrypoint
RUN chmod +x /usr/local/bin/usrp-sim-entrypoint
ENV PYTHONPATH=/opt/usrp-sim PYTHONUNBUFFERED=1
USER usrp-sim
VOLUME ["/config", "/var/lib/usrp-sim"]
# X300: 49152-49153/udp, 49158/udp   MPMD (profile = "custom"): 49600/udp, 49601/tcp, 49153/udp
EXPOSE 49152/udp 49153/udp 49158/udp 49600/udp 49601/tcp
STOPSIGNAL SIGINT
ENTRYPOINT ["usrp-sim-entrypoint"]
