#!/bin/sh
# One D-Bus, one Avahi and one nqptp serve every AirPlay speaker; the bridge starts a shairport-sync per linked TV.
set -e

OPTIONS=/data/options.json
PORT=$(python3 -c "import json; print(json.load(open('$OPTIONS')).get('bridge_port', 7700))" 2>/dev/null || echo 7700)
IFACE=$(ip route show default 2>/dev/null | awk '{print $5; exit}')

rm -rf /run/dbus/dbus.pid /run/avahi-daemon/pid
dbus-uuidgen --ensure
dbus-daemon --system

# Avahi only on the home network: on Docker bridges it collides with itself and runs out of D-Bus objects
if [ -n "$IFACE" ]; then
  sed -i "s/^#\{0,1\}allow-interfaces=.*/allow-interfaces=$IFACE/" /etc/avahi/avahi-daemon.conf
fi

# The TVs find the bridge by this service
mkdir -p /etc/avahi/services
cat > /etc/avahi/services/steklo-airplay.service <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>Steklo AirPlay</name>
  <service>
    <type>_steklo-airplay._tcp</type>
    <port>$PORT</port>
  </service>
</service-group>
EOF

avahi-daemon --daemonize --no-chroot
while [ ! -f /var/run/avahi-daemon/pid ]; do sleep 1; done

# Timing for AirPlay 2; it needs UDP ports 319 and 320, which the host network gives the add-on
(/usr/local/bin/nqptp > /dev/null 2>&1) &

echo "Steklo AirPlay: Avahi on ${IFACE:-all interfaces}, bridge on port $PORT"
exec python3 /steklo/bridge.py
