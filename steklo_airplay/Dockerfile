FROM mikebrady/shairport-sync:5.6.2

# Python runs the bridge; dbus-send from the image reaches each speaker's remote control
RUN apk add --no-cache python3

COPY run.sh bridge.py /steklo/
RUN chmod +x /steklo/run.sh

ENTRYPOINT ["/steklo/run.sh"]
