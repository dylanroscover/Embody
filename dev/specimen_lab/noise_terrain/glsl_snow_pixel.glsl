uniform vec4 uTerrain;   // x = snow line base (was 1.6), y = bump strength (was 0.17), z = haze multiplier, w = unused
uniform vec4 uSunDir;    // xyz = sun light direction (unit, from az/el binding), w = unused
in vec3 iWorldPos;
in vec3 iWorldNorm;
out vec4 oFragColor;
float hash(vec3 p){ return fract(sin(dot(p,vec3(127.1,311.7,74.7)))*43758.5453); }
float vnoise(vec3 p){ vec3 i=floor(p),f=fract(p); f=f*f*(3.0-2.0*f);
  return mix(mix(mix(hash(i+vec3(0,0,0)),hash(i+vec3(1,0,0)),f.x),mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
             mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z); }
float fbm(vec3 p){ float v=0.0,a=0.5; for(int i=0;i<4;i++){v+=a*vnoise(p); p=p*2.03+vec3(1.7); a*=0.5;} return v; }
float fbm3(vec3 p){ float v=0.0,a=0.5; for(int i=0;i<3;i++){v+=a*vnoise(p); p=p*2.05+vec3(1.7); a*=0.5;} return v; }
vec3 bumpN(vec3 P,vec3 N,float fr,float st){ float e=0.03;
  float h=fbm3(P*fr),hx=fbm3((P+vec3(e,0,0))*fr),hz=fbm3((P+vec3(0,0,e))*fr);
  return normalize(N+vec3((h-hx),0.0,(h-hz))/e*st); }
void main(){
    TDCheckDiscard();
    vec3 P=iWorldPos; vec3 Ng=normalize(iWorldNorm);
    float slopeG=clamp(Ng.y,0.0,1.0);
    float snowLine=uTerrain.x+(fbm(P*0.4)-0.5)*1.1;
    float snowMask=clamp(smoothstep(snowLine-0.5,snowLine+0.5,P.y),0.0,1.0);
    vec3 N=bumpN(P,Ng,9.0,uTerrain.y*(1.0-0.85*snowMask));
    float slope=clamp(N.y,0.0,1.0);
    float c1=fbm(P*1.3), c2=fbm(P*3.2+5.0), c3=fbm(P*0.7-3.0); float fine=fbm(P*4.0);
    vec3 rock=vec3(0.46,0.34,0.24);
    rock=mix(rock,vec3(0.34,0.37,0.40),smoothstep(0.35,0.75,c1));
    rock=mix(rock,vec3(0.44,0.22,0.16),smoothstep(0.55,0.85,c2)*0.6);
    rock=mix(rock,vec3(0.28,0.34,0.20),smoothstep(0.58,0.88,c3)*0.4);
    rock*=0.72+0.28*fine;
    vec3 snow=vec3(0.88,0.90,0.95);
    vec3 albedo=mix(rock,snow,snowMask);
    vec3 L=normalize(uSunDir.xyz);
    float ndl=max(dot(N,L),0.0);
    vec3 sun=vec3(1.55,1.5,1.35)*ndl*(1.0-0.55*snowMask);  // expose snow below clipping, keep shading
    vec3 sky=vec3(0.40,0.52,0.74)*(0.42+0.58*slope);
    vec3 col=albedo*(sun+sky);
    col+=pow(ndl,60.0)*snowMask*0.18;
    col=(col-0.45)*1.12+0.45;
    float dist=length(P-vec3(0.0,6.2,16.0));
    float depth=clamp((dist-9.0)/19.0,0.0,1.0);                                    // 0 = foreground (clear), 1 = far
    float distFog=pow(depth,1.7)*0.82;                                             // power curve: clear fg, builds with depth
    float valleyMist=exp(-max(P.y-0.2,0.0)*0.7)*0.18*smoothstep(0.25,0.75,depth);  // mist only in distant valleys
    float atmo=clamp((distFog+valleyMist)*uTerrain.z,0.0,0.88);
    col=mix(col,vec3(0.66,0.78,0.92),atmo);
    oFragColor=TDOutputSwizzle(vec4(clamp(col,0.0,2.0),1.0));
}
