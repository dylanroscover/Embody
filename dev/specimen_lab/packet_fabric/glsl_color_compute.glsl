// Packet Fabric (Vertical) -- per-packet stream color + PTP sync flash. (render side branch)
// uClock = (ptpRate, time, streams, pulseStrength)
// New attributes Color (vec4) and PointScale (float) come from the Attr sequence.
float hash11(float p){ return fract(sin(p*127.1)*43758.5453); }

void main(){
    const uint id = TDIndex();
    if(id >= TDNumElements()) return;

    vec3 pos = TDIn_P(0, id);
    float pid = float(id);

    float streams = max(1.0, uClock.z);
    float klass   = floor(mod(pid, streams));   // 0=video,1=audio,2=ancillary

    vec3 cyan  = vec3(0.00, 0.90, 1.00);   // video essence
    vec3 green = vec3(0.22, 1.00, 0.42);   // audio essence
    vec3 amber = vec3(1.00, 0.69, 0.00);   // ancillary data
    vec3 col = (klass < 0.5) ? cyan : (klass < 1.5) ? green : amber;
    col = mix(col, vec3(1.0), 0.35);       // white-hot core lift; palette carries mid-glow

    // PTP grandmaster pulse: a steady beat all packets flash to, in lockstep.
    float rate  = max(0.05, uClock.x);
    float beat  = fract(uClock.y * rate);
    float flash = pow(max(0.0, 1.0 - beat*3.0), 3.0);
    float pulse = 1.0 + uClock.w * flash;

    Color[id]      = vec4(col * (0.45 * pulse), 1.0);
    PointScale[id] = 0.8 + 0.7*flash;      // packets swell on the sync beat
}
