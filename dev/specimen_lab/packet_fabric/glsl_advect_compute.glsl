// Packet Fabric (Vertical) -- advect each packet DOWN an assigned vertical link, recycle at the bottom.
// uFlow  = (flowSpeed, linkCount, curveAmount, time)
// uClock = (ptpRate, fieldScale, streams, jitter)
// Uniforms are AUTO-declared from the Vectors page -- do NOT redeclare here.

float hash11(float p){ return fract(sin(p*127.1)*43758.5453); }
vec3  hash31(float p){ return fract(sin(vec3(p*127.1, p*311.7, p*74.7))*43758.5453)*2.0-1.0; }

// a node anchor; senders live near the TOP (+Y), receivers near the BOTTOM (-Y).
vec3 nodeAnchor(float n, float scale, float yLevel){
    vec3 h = hash31(n*13.0 + 1.0);
    // spread nodes mostly along X (a tall portrait field), fixed Y level, small Z scatter
    return vec3(h.x*0.9*scale, yLevel, h.z*0.5*scale);
}

void main(){
    const uint id = TDIndex();
    if(id >= TDNumElements()) return;

    float t      = uFlow.w;
    float speed  = uFlow.x;
    float linkN  = max(2.0, uFlow.y);
    float curve  = uFlow.z;
    float scale  = uClock.y;
    float jitter = uClock.w;

    float pid = float(id);

    // assign this packet to a vertical link (a top sender + a bottom receiver)
    float link    = floor(mod(pid, linkN));
    float srcNode = floor(mod(link*2.0,   linkN+7.0));
    float dstNode = floor(mod(link*2.0+1.0, linkN+11.0));
    vec3  A = nodeAnchor(srcNode, scale,  1.7*scale);   // sender (top)
    vec3  B = nodeAnchor(dstNode, scale, -1.7*scale);   // receiver (bottom)

    // a gentle horizontal control point so the link bows (fiber slack)
    vec3  mid = mix(A, B, 0.5);
    vec3  bow = vec3((hash11(link*7.3)*2.0-1.0)*1.0, 0.0, (hash11(link*3.1)*2.0-1.0)*0.6) * curve;
    vec3  C   = mid + bow;

    // per-packet parametric position: travels DOWN the link (s: 0 top -> 1 bottom), wraps.
    float jit = (hash11(pid*1.7)*2.0-1.0) * 0.08 * jitter;
    float s   = fract(hash11(pid*9.13) + (t*speed*(0.6+0.5*hash11(pid*4.2))) + jit);

    // quadratic Bezier A(top) -> C -> B(bottom): s grows -> packet descends
    float u  = 1.0 - s;
    vec3 pos = u*u*A + 2.0*u*s*C + s*s*B;

    P[id] = pos;
}
