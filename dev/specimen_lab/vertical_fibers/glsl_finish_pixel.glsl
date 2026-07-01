// Cheap O(1) finish: vertical scanline + grain + vignette. No loops.
uniform vec4 uFin;   // x=grain, y=vignette, z=absTime
out vec4 fragColor;
float hash21(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
void main(){
    vec2 uv = vUV.st;
    vec3 col = texture(sTD2DInputs[0], uv).rgb;
    // faint vertical scanline (columns of fiber) -- subtle structure, not a CRT
    float scan = 0.985 + 0.015*sin(uv.x * uTDOutputInfo.res.z * 0.5);
    col *= scan;
    // vignette toward the frame edges (premium dark stage)
    vec2 p = uv - 0.5;
    float vig = 1.0 - uFin.y * smoothstep(0.35, 0.85, length(vec2(p.x*0.6, p.y)));
    col *= vig;
    // grain (kills banding on the dark gradient at booth scale)
    col += (hash21(uv*uTDOutputInfo.res.zw + uFin.z*60.0)-0.5) * 0.012 * uFin.x;
    fragColor = vec4(col, 1.0);
}
