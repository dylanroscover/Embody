out vec4 fragColor;
void main(){
    vec2 uv = vUV.st;
    vec2 s = texture(sTD2DInputs[0], uv).rg;
    float b = s.g;
    vec3 c0 = vec3(0.03, 0.04, 0.09);
    vec3 c1 = vec3(0.10, 0.42, 0.55);
    vec3 c2 = vec3(0.98, 0.90, 0.70);
    vec3 col = mix(c0, c1, smoothstep(0.0, 0.35, b));
    col = mix(col, c2, smoothstep(0.35, 0.55, b));
    float d = distance(uv, vec2(0.5));
    col *= mix(1.0, 0.55, smoothstep(0.40, 0.95, d));
    fragColor = vec4(col, 1.0);
}
