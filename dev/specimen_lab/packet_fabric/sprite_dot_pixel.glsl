out vec4 fragColor;
void main(){
    vec2 uv = vUV.st * 2.0 - 1.0;
    float r = length(uv);
    float a = smoothstep(1.0, 0.05, r);   // bright round core, soft falloff edge
    a = pow(a, 1.5);
    fragColor = vec4(vec3(a), 1.0);
}
