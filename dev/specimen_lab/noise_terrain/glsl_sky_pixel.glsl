out vec4 fragColor;
float h2(vec2 p){ return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453); }
float n2(vec2 p){ vec2 i=floor(p),f=fract(p); f=f*f*(3.0-2.0*f);
  return mix(mix(h2(i),h2(i+vec2(1,0)),f.x),mix(h2(i+vec2(0,1)),h2(i+vec2(1,1)),f.x),f.y); }
float fbm2(vec2 p){ float v=0.0,a=0.5; for(int i=0;i<4;i++){v+=a*n2(p); p*=2.0; a*=0.5;} return v; }
void main(){
    vec2 uv=vUV.st; float y=uv.t;
    vec3 zenith=vec3(0.17,0.42,0.83); vec3 horizon=vec3(0.74,0.84,0.93);
    vec3 col=mix(horizon,zenith,smoothstep(0.0,0.95,y));
    float cl=fbm2(vec2(uv.x*3.5,uv.y*6.0));
    col=mix(col,vec3(0.97,0.98,1.0),smoothstep(0.6,0.85,cl)*0.45);
    fragColor=vec4(col,1.0);
}
