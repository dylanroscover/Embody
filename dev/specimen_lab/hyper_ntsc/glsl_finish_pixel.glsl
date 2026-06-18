// NTSC finish: fine grain + vignette over the composited (sharp + bloom) image.
uniform vec4 uFin;   // x=grain, y=vignette, z=unused, w=absTime
out vec4 fragColor;
float h21(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
void main(){
    vec2 uv = vUV.st;
    vec3 col = texture(sTD2DInputs[0], uv).rgb;
    col += (h21(uv*uTDOutputInfo.res.zw + uFin.w*30.0)-0.5)*0.02*uFin.x;
    float d = length((uv-0.5)*vec2(1.1,1.0));
    col *= 1.0 - uFin.y*0.5*smoothstep(0.45, 0.95, d);
    fragColor = vec4(max(col,0.0), 1.0);
}