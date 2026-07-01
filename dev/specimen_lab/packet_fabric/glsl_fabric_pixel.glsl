// Static vertical IP-fabric background: sender row (top) + receiver row (bottom) + faint vertical links.
// Cheap: a SMALL constant-bounded link loop (links are few), no per-pixel mass loop.
uniform vec4 uFab;   // x=linkCount, y=fieldScale, z=slowTime, w=ptpRate
out vec4 fragColor;
float hash21(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
float segDist(vec2 p, vec2 a, vec2 b){
    vec2 pa=p-a, ba=b-a; float h=clamp(dot(pa,ba)/dot(ba,ba),0.0,1.0); return length(pa-ba*h);
}
void main(){
    vec2 uv = vUV.st;
    float aspect = uTDOutputInfo.res.z / uTDOutputInfo.res.w;
    vec2 p = (uv - 0.5); p.x *= aspect;     // centered, y up

    vec3 col = mix(vec3(0.012,0.014,0.018), vec3(0.026,0.030,0.038), 1.0-length(p));

    float linkN = clamp(uFab.x, 4.0, 24.0);
    vec3 fiber = vec3(0.0);
    const int MAXL = 24;
    for(int i=0;i<MAXL;i++){
        if(float(i) >= linkN) break;
        float fi = float(i);
        // sender near the top, receiver near the bottom -> VERTICAL links
        vec2 a = vec2((hash21(vec2(fi,1.0))-0.5)*0.9*aspect,  0.42);  // top
        vec2 b = vec2((hash21(vec2(fi,3.0))-0.5)*0.9*aspect, -0.42);  // bottom
        float d = segDist(p, a, b);
        fiber += vec3(0.10,0.13,0.16) * smoothstep(0.010, 0.0, d) * 0.5;
        float tw = 0.7 + 0.3*sin(uFab.z*6.2831 + fi*2.0);
        fiber += vec3(0.12,0.16,0.20) * smoothstep(0.045,0.0,length(p-a)) * tw;  // sender node
        fiber += vec3(0.12,0.16,0.20) * smoothstep(0.045,0.0,length(p-b)) * tw;  // receiver node
    }
    col += fiber;

    // depth haze: dim toward the frame edges
    col *= mix(1.0, 0.55, smoothstep(0.4, 1.1, length(p)));
    // grain to kill banding on the dark gradient at booth scale
    col += (hash21(uv*uTDOutputInfo.res.zw + uFab.z*100.0) - 0.5) * 0.012;
    fragColor = vec4(max(col, 0.0), 1.0);
}
