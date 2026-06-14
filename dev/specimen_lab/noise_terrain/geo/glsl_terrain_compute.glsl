// Ridged multifractal terrain -- GPU geometry generator (replaces fbm noisePOPs).
// Uniforms auto-declared from the Vectors page: uShape=(Height,Ridge,Warp,Seed), uTime=(time,..).
// 3D value noise: the 3rd coord = time -> terrain morphs IN PLACE (not a pan); per-octave rate = multi-scale.
float hash3(vec3 p){ p=fract(p*0.3183099+vec3(0.1,0.2,0.3)); p*=17.0; return fract(p.x*p.y*p.z*(p.x+p.y+p.z)); }
float vnoise3(vec3 p){
  vec3 i=floor(p), f=fract(p); vec3 u=f*f*(3.0-2.0*f);
  return mix(mix(mix(hash3(i+vec3(0,0,0)),hash3(i+vec3(1,0,0)),u.x),
                 mix(hash3(i+vec3(0,1,0)),hash3(i+vec3(1,1,0)),u.x),u.y),
             mix(mix(hash3(i+vec3(0,0,1)),hash3(i+vec3(1,0,1)),u.x),
                 mix(hash3(i+vec3(0,1,1)),hash3(i+vec3(1,1,1)),u.x),u.y),u.z);
}
float ridged(vec2 p, float sharp, vec2 so, float t){
  float sum=0.0, freq=1.0, amp=0.62, prev=1.0;
  for(int i=0;i<3;i++){
    float tz = t*(0.05+0.045*float(i));            // per-octave morph rate -> earthquake undercurrents
    float n=vnoise3(vec3(p*freq+so, tz));
    n=1.0-abs(2.0*n-1.0);                           // ridge transform -> creases
    n=pow(n,sharp);                                 // sharpen ridgelines
    sum+=n*amp*clamp(prev,0.0,1.0);                 // multifractal: rough peaks, smooth valleys
    prev=n; freq*=2.0; amp*=0.45;
  }
  return sum;
}
void main(){
  const uint id=TDIndex();
  if(id>=TDNumElements()) return;
  vec3 pos=TDIn_P(0, id);
  float Height=uShape.x, Ridge=uShape.y, Warp=uShape.z, Seed=uShape.w;
  float t=uTime.x;
  vec2 so=vec2(Seed*19.3, Seed*7.7);               // seed -> a different mountain
  vec2 xz=pos.xz*0.18;                             // domain scale
  vec2 w=vec2(vnoise3(vec3(xz*0.6+so, t*0.03)), vnoise3(vec3(xz*0.6+so+5.2, t*0.03+2.0)));
  xz+=(w-0.5)*Warp;                               // domain warp -> organic, non-griddy
  float h=ridged(xz, Ridge, so, t);
  pos.y+=(h-0.35)*4.6*Height;                     // offset down for rock/snow contrast, then scale
  P[id]=pos;
}