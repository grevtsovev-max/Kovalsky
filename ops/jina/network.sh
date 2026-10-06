#!/bin/sh
set -eu
docker network inspect kovalsky-reader >/dev/null 2>&1 || docker network create --subnet 172.30.89.0/28 kovalsky-reader >/dev/null
iptables -N KOVALSKY_READER 2>/dev/null || true
iptables -F KOVALSKY_READER
iptables -A KOVALSKY_READER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for subnet in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4; do
 iptables -A KOVALSKY_READER -d "$subnet" -j REJECT
done
iptables -A KOVALSKY_READER -j RETURN
iptables -C DOCKER-USER -s 172.30.89.2 -j KOVALSKY_READER 2>/dev/null || iptables -I DOCKER-USER 1 -s 172.30.89.2 -j KOVALSKY_READER
iptables -C INPUT -s 172.30.89.2 -m conntrack --ctstate NEW -j REJECT 2>/dev/null || iptables -I INPUT 1 -s 172.30.89.2 -m conntrack --ctstate NEW -j REJECT
