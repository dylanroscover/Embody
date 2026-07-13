out vec4 fragColor;
float h(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
void main(){
    vec2 uv = vUV.st;
    float a = 1.0; float b = 0.0;
    vec2 g = uv * 7.0;
    vec2 cell = floor(g);
    vec2 f = fract(g);
    vec2 jit = vec2(h(cell), h(cell+3.7))*0.6 + 0.2;
    if(distance(f, jit) < 0.13) b = 1.0;
    fragColor = vec4(a, b, 0.0, 1.0);
}
