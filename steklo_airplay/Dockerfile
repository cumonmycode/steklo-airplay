FROM mikebrady/shairport-sync:5.6.2

# Python runs the bridge; avahi-browse finds the iPhone's remote-control service for the TV's buttons
RUN apk add --no-cache python3 avahi-tools

COPY run.sh bridge.py /steklo/
RUN chmod +x /steklo/run.sh

ENTRYPOINT ["/steklo/run.sh"]
