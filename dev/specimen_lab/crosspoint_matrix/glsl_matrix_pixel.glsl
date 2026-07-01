// Crosspoint Matrix -- NxN routing grid with igniting crosspoints + L-path packets.
uniform vec4 uGrid;    // x=gridSize(N), y=activeRoutes, z=latticeGlow, w=crosspointGlow
uniform vec4 uPacket;  // x=packetSpeed, y=packetDensity, z=streams, w=time(=absTime*packetSpeed)
uniform vec4 uClock;   // x=ptpRate, y=reconfigRate, z=lockIndicators, w=absTime
out vec4 fragColor;
const float PI = 3.14159265;

float hash11(float p){ return fract(sin(p*127.1)*43758.5453); }

// matrix lives in a centered square region of the frame; map uv -> matrix space [0,1]^2
// returns matrix coords; the .xy in [0,1] over the grid area
vec2 toMatrix(vec2 uv, float aspect, out float inside){
    // place a square matrix in the middle, with margins for ports/labels
    vec2 m = (uv - vec2(0.09, 0.08)) / vec2(0.82, 0.84);  // map to [0,1]
    inside = step(0.0, m.x)*step(m.x,1.0)*step(0.0,m.y)*step(m.y,1.0);
    return m;
}

float segDistAxis(float p, float a, float b, float hw){
    // distance to a 1D segment [a,b] -- returns 0 inside, grows outside
    float lo = min(a,b)-hw, hi=max(a,b)+hw;
    return max(max(lo - p, p - hi), 0.0);
}

void main(){
    vec2 uv = vUV.st;
    float aspect = uTDOutputInfo.res.z / uTDOutputInfo.res.w;

    vec3 col = vec3(0.010, 0.012, 0.016);   // near-black engineering ground

    float inside;
    vec2 m = toMatrix(uv, aspect, inside);

    float N    = clamp(uGrid.x, 4.0, 40.0);
    float nR   = clamp(uGrid.y, 1.0, 20.0);
    float lat  = uGrid.z;
    float cpg  = uGrid.w;
    float t    = uPacket.w;
    float pdens= uPacket.y;
    float streams = max(1.0, uPacket.z);

    // cell coordinates within the matrix
    vec2 cell = m * N;                       // 0..N
    vec2 cellId = floor(cell);
    vec2 cellF = fract(cell);

    // --- dim disciplined lattice (cool gray) ---
    vec2 lines = abs(cellF - 0.5);
    float grid = smoothstep(0.5, 0.486, max(lines.x, lines.y));
    col += vec3(0.14, 0.20, 0.28) * grid * 0.5 * lat * inside;

    // --- input ports (left edge) + output ports (top edge) ---
    float portL = smoothstep(0.016, 0.0, abs(uv.x - 0.085)) * step(0.08, uv.y) * step(uv.y, 0.92);
    float portT = smoothstep(0.016, 0.0, abs(uv.y - 0.915)) * step(0.09, uv.x) * step(uv.x, 0.91);
    col += vec3(0.12, 0.16, 0.20) * (portL + portT) * 0.32;

    // essence stream colors
    vec3 streamCol[3];
    streamCol[0] = vec3(0.00, 0.90, 1.00);   // cyan video
    streamCol[1] = vec3(0.22, 1.00, 0.42);   // green audio
    streamCol[2] = vec3(1.00, 0.69, 0.00);   // amber ancillary

    // --- active routes: hashed schedule, cycling on the reconfig beat ---
    // (per-route independent lifecycle computed inside the loop -- no global reconfig fade)

    const int MAXR = 20;
    for(int r=0; r<MAXR; r++){
        if(float(r) >= nR) break;
        float fr = float(r);
        // each route fades in/out on its OWN random cycle (staggered) -- never all at once
        float rT     = uClock.w * uClock.y * (0.55 + 0.9*hash11(fr*13.0)) + hash11(fr*5.7);
        float rEpoch = floor(rT);
        float rPhase = fract(rT);
        float fade   = smoothstep(0.0, 0.10, rPhase) * (1.0 - smoothstep(0.80, 1.0, rPhase));
        float i = floor(hash11(rEpoch*7.0 + fr*3.1) * N);                          // re-routes when it cycles
        float j = mod(floor(fr * N / max(nR,1.0)) + floor(hash11(fr*9.7)*3.0), N); // stable column per route
        vec3 sc = streamCol[int(mod(fr, streams))];

        // crosspoint cell center in matrix space
        vec2 cp = (vec2(j, i) + 0.5) / N;   // x follows column j, y follows row i

        if(inside > 0.5){
            // row segment: along x at row i, from input edge (x=0) to column j
            float rowY = (i + 0.5)/N;
            float dRow = max(abs(m.y - rowY) - 0.0015, 0.0) + segDistAxis(m.x, 0.0, cp.x, 0.0);
            float litRow = smoothstep(0.006, 0.0, dRow);
            // column segment: along y at column j, from row i up to output edge (y=1)
            float colX = (j + 0.5)/N;
            float dCol = max(abs(m.x - colX) - 0.0015, 0.0) + segDistAxis(m.y, cp.y, 1.0, 0.0);
            float litCol = smoothstep(0.006, 0.0, dCol);
            // crosspoint node ignite
            float node = smoothstep(0.016, 0.0, length(m - cp));

            float lit = (litRow + litCol + node*2.0) * fade;
            col += sc * lit * 0.85 * cpg;
            col += vec3(1.0) * node * fade * cpg * 1.15;   // white-hot crosspoint core

            // --- packets travel the L-path: along the row to the crosspoint, then up the column ---
            float rowLen = cp.x;            // length of row segment (x distance)
            float colLen = 1.0 - cp.y;      // length of column segment (y distance)
            float total = rowLen + colLen + 1e-4;
            for(int k=0;k<10;k++){
                if(float(k) >= pdens) break;
                float fk = float(k);
                float s = fract(hash11(fr*4.0+fk*2.7) + t*(0.6+0.4*hash11(fr+fk)));
                float along = s * total;    // arc length traveled
                vec2 pp;
                if(along < rowLen){ pp = vec2(along, rowY); }                 // on the row
                else { pp = vec2(cp.x, rowY + (along - rowLen)); }            // on the column (upward)
                // note rowY==cp.y so the turn is continuous
                float dpk = length(m - pp);
                float pk = exp(-dpk*dpk*4000.0);
                vec3 core = mix(sc, vec3(1.0), 0.6);
                col += core * pk * fade * 2.4;
            }
        }
    }

    // --- corner PTP-lock indicators (live telemetry feel) ---
    float beat = fract(uClock.w * uClock.x);
    float lockPulse = 0.5 + 0.5*pow(max(0.0,1.0-beat*4.0),2.0);
    float lock = smoothstep(0.009,0.0,length(uv-vec2(0.115,0.885)))
               + smoothstep(0.009,0.0,length(uv-vec2(0.885,0.885)));
    col += vec3(0.20,1.0,0.45) * lock * lockPulse * uClock.z;   // green PTP-lock dots

    // edge falloff so the matrix sits on a premium dark stage
    col *= mix(1.0, 0.7, smoothstep(0.5, 1.0, length(uv-0.5)));

    fragColor = vec4(max(col, 0.0), 1.0);
}
