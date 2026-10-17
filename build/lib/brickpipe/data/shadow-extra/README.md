# Shadow supplement

Connector records for LDraw primitives and parts that LDraw Nova's copy of
the LDCad shadow library does not cover. Same format as the shadow library
(`0 !LDCAD SNAP_*` lines), consulted only when the shadow library has no file
of that name.

| File | Why |
|---|---|
| `p/wpinhol2.dat` | "Wheel Hole Notched over Cylinder", the newer notched wheel hole used by rims such as 6014b. Same 1 LDU, radius 4 hole as `wpinhole.dat`, scaled along its axis by the part. Without it a rim on a wheel pin is only held by the press-fit rule. |

Found gaps are also worth sending upstream to the LDCad shadow library.
