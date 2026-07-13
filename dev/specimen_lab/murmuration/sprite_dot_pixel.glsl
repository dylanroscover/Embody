out vec4 fragColor;
void main(){
    vec2 uv = vUV.st * 2.0 - 1.0;
    float r = length(uv);
    float a = smoothstep(1.0, 0.15, r);   // bright full core, soft round edge
    fragColor = vec4(vec3(a), 1.0);
}
