# Operator station — network setup

Run the operator station from any computer and point Scout at it. Nothing about the
operator PC is hardcoded — the vehicle is told where to post at runtime.

## 1. Start the backend on the operator PC

```powershell
./run_operator_backend.ps1
```

This binds `0.0.0.0:8200` (all interfaces) so Scout can reach it over the network,
and prints this PC's LAN addresses. Equivalent manual command:

```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8200 --no-access-log
```

Open the station locally at http://127.0.0.1:8200/app/ to confirm it's up.

## 2. Find the operator PC's IP

`run_operator_backend.ps1` prints the addresses on startup. To find it manually:

```powershell
ipconfig
```

Use the IPv4 address on the **same network as Scout / the router** (e.g. the
`192.168.x.x` or `10.x.x.x` on the vehicle's Wi-Fi/4G router — not `127.0.0.1` and not
a `169.254.x.x` link-local or a Hyper-V/WSL virtual switch address).

## 3. Point Scout at this operator PC

On Scout, set the Local Agent's `OPERATOR_URLS` to this PC's address:

```
OPERATOR_URLS=http://<operator-ip>:8200
```

Replace `<operator-ip>` with the address from step 2 (e.g.
`http://192.168.0.168:8200`). Restart the Local Agent so it picks up the new target.

## 4. Verify from Scout

From a shell on Scout (`ssh motherpi@10.0.2.10` — see the Terminal page), confirm the
operator backend is reachable:

```bash
curl http://<operator-ip>:8200/api/fleet/status
```

You should get the fleet JSON (three template vehicles, or Scout live once the agent
is posting). If it hangs or is refused, it's a network/firewall issue between Scout
and the operator PC — not the app:

- Both must be on the **same reachable network** (same router / VPN / subnet).
- Allow inbound TCP **8200** through the operator PC's firewall (Windows may prompt to
  allow Python on first run — choose the network profile you're on).
- Re-check the operator IP from step 2; laptop/desktop addresses change between
  networks, so it will differ each time you move machines.

## Notes

- The operator station makes no assumption about which computer it runs on — moving to
  a different laptop/desktop only means repeating steps 2–3 with the new IP.
- This is documentation and a startup helper only: there is no in-app operator-computer
  selection and no backend discovery.
