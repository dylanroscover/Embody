uniform vec4 uPal;   // x = hue 0..1
out vec4 fragColor;
float hash(vec2 p){ return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453); }
float noise(vec2 p){ vec2 i=floor(p),f=fract(p); vec2 u=f*f*(3.0-2.0*f);
  return mix(mix(hash(i),hash(i+vec2(1,0)),u.x),mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),u.x),u.y); }
float fbm(vec2 p){ float v=0.0,a=0.5; for(int i=0;i<5;i++){v+=a*noise(p);p*=2.0;a*=0.5;} return v; }
void main(){
    vec2 uv=vUV.st; vec2 p=uv*3.0;
    float n1=fbm(p);
    float n2=fbm(p*1.8 + n1*2.0);
    vec3 col = 0.5 + 0.5*cos(vec3(0.0,0.9,1.9) + n2*3.2 + uPal.x*6.2831);
    col = mix(vec3(dot(col,vec3(0.299,0.587,0.114))),col,1.35);
    fragColor=vec4(clamp(col,0.0,1.0),1.0);
}