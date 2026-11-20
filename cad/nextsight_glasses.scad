/* ================================================================
   NEXTSIGHT SMART GLASSES — 3D-PRINTABLE FRAME
   Parametric OpenSCAD Design v1.0
   
   Target Printer : Creality K2 Plus
   Material       : Generic PLA (1.75mm)
   Hinge System   : Filament Pin (1.75mm raw PLA as pivot)
   Design Style   : Ergonomic full-rim with hull() organic transitions
   
   Coordinate Convention:
     X = Left ↔ Right  (positive = wearer's left)
     Y = Front ↔ Back  (positive = toward wearer / backward)
     Z = Down ↔ Up     (positive = upward)
   
   PRINT NOTES:
     • Front frame: print flat (XY plane on bed, Z up)
     • Temples: print on inner face (flat side down)
     • Layer height: 0.16mm for hinge barrel precision
     • Infill: 30% gyroid for strength-to-weight
     • Walls: 3 perimeters (≥1.2mm total wall)
   ================================================================ */


// ========================== RENDER QUALITY ===========================
// Set $fn=30 for fast preview (F5), $fn=80 for final render (F6)
$fn = 60;


// ======================== GLOBAL PARAMETERS ==========================
wall       = 2.0;     // Minimum wall thickness around components
fillet_r   = 2.0;     // Global corner fillet radius for aesthetic rounding
tol        = 0.3;     // Printer tolerance / fit clearance per side
gap        = 0.4;     // Hinge interleave clearance

// ====================== FRONT FRAME PARAMETERS =======================
frame_total_w  = 142;     // Overall frame width (eye-to-eye outer)
frame_h        = 40;      // Frame height (Z)
frame_depth    = 8;       // Frame depth / thickness (Y)
bridge_w       = 18;      // Bridge span between lenses
lens_w         = 48;      // Lens opening width
lens_h         = 30;      // Lens opening height
rim_w          = 5;       // Rim material width around lens
wire_ch_w      = 3.5;     // Wire channel groove width
wire_ch_d      = 2.5;     // Wire channel groove depth into top surface
cam_window_d   = 5.5;     // Camera lens window clear aperture diameter

// Computed lens center X offset from origin
lens_cx = bridge_w/2 + rim_w + lens_w/2;  // ≈ 38mm

// ======================== HINGE PARAMETERS ===========================
hinge_od          = 5.5;    // Barrel outer diameter
hinge_id          = 2.1;    // Bore for 1.75mm filament (0.35mm total play)
hinge_frame_bh    = 3.5;    // Frame-side barrel height (× 2 barrels)
hinge_temple_bh   = 4.0;    // Temple-side barrel height (1 barrel)
hinge_total_h     = 2*hinge_frame_bh + hinge_temple_bh; // 11mm stack
hinge_arm_len     = 6;      // Arm connecting barrel to frame/temple body
hinge_arm_w       = 4;      // Arm width

// Hinge position: at outer edge of front frame, vertically centered
hinge_x = frame_total_w/2 + 1;  // Slightly beyond frame edge
hinge_z = 0;                     // Vertically centered on frame

// ================== COMPONENT DIMENSIONS (DATASHEET) =================
// All heights include +8.5mm for soldered male header pins where applicable

// XIAO ESP32S3 Sense (transverse mount)
xiao_l     = 21.0;    // → becomes temple HEIGHT (Z) when transverse
xiao_w     = 17.8;    // → becomes temple DEPTH (Y) at front
xiao_h_pcb = 15.0;    // PCB+camera+heatsink stack (no headers)
xiao_h_tot = 23.5;    // With soldered headers extending backward
xiao_cam_d = 4.0;     // Camera lens module diameter
xiao_usbc_w = 9.0;    // USB-C port width
xiao_usbc_h = 3.5;    // USB-C port height

// INMP441 MEMS Microphone breakout
inmp_l = 14.0;
inmp_w = 14.0;
inmp_h = 11.0;        // With headers
inmp_port_d = 3.0;    // Sound port hole diameter (bottom face)

// MAX98357A I2S Amplifier breakout (Adafruit)
max_l = 19.4;
max_w = 17.8;
max_h = 11.5;         // With headers

// Micro Speaker (15mm round, 8Ω 1W)
spk_d = 15.0;
spk_h = 4.5;
spk_port_d = 10.0;    // Sound port array diameter (downward facing)

// TTP223 Capacitive Touch Sensor (mini variant)
ttp_l = 15.0;
ttp_w = 11.0;
ttp_h = 10.3;         // With headers

// TP4056 Type-C Charging Module (with protection)
tp_l = 25.0;
tp_w = 17.0;
tp_h = 12.5;          // With headers
tp_usbc_w = 9.0;
tp_usbc_h = 3.5;

// 160mAh 3.7V LiPo Battery (model 401020)
bat_l = 22.0;          // +2mm for wires/connector
bat_w = 10.0;
bat_h = 4.5;           // With 10% swell tolerance

// SS12D00 Mini Slider Switch (3-pin SPDT)
sw_l = 8.0;
sw_w = 4.0;
sw_body_h = 3.5;
sw_knob_h = 3.0;      // Knob protrusion below body
sw_total_h = sw_body_h + sw_knob_h;

// ============== RIGHT TEMPLE CROSS-SECTION PROFILE ===================
// Outer dimensions [y_position, width_X, height_Z]
// Width = side-to-side thickness; Height = top-to-bottom
rt_sections = [
    // Y,    W,    H
    [  0,   22,   26],    // Zone A start — XIAO mount (widest point)
    [ 26,   22,   22],    // Zone A end
    [ 42,   18,   17],    // Zone B end — INMP441
    [ 60,   16,   16],    // Zone C end — TTP223
    [ 85,   15,   15],    // Zone D end — MAX98357A + Speaker
    [108,   14,   14],    // Straight section before ear hook
    [125,   13,   12],    // Ear hook entry
];

// Ear hook: gentle 120° downward bend at the tail
ear_hook_len     = 18;     // Arc length of the ear hook
ear_hook_angle   = 100;    // Total bend angle in degrees
ear_hook_steps   = 6;      // Subdivision steps for smooth curve
ear_hook_end_w   = 10;     // Width at very tip
ear_hook_end_h   = 9;      // Height at very tip

// ============== LEFT TEMPLE CROSS-SECTION PROFILE ====================
lt_sections = [
    [  0,   17,   22],    // Zone A start — switch mount
    [ 14,   17,   20],    // Zone A end
    [ 42,   15,   16],    // Zone B end — battery
    [ 72,   16,   16],    // Zone C end — TP4056
    [108,   14,   14],    // Straight section before ear hook
    [125,   13,   12],    // Ear hook entry
];


// ========================= HELPER MODULES ============================

// 2D rounded rectangle centered at origin
module rrect_2d(w, h, r) {
    offset(r=r) offset(delta=-r)
        square([w, h], center=true);
}

// 3D rounded box centered at origin (via minkowski — use sparingly)
module rbox(w, h, d, r) {
    minkowski() {
        cube([w - 2*r, h - 2*r, d - 2*r], center=true);
        sphere(r=r, $fn=max(16, $fn/2));
    }
}

// Hull between two cross-section slices positioned along Y axis
// Slices are thin linear extrusions of rounded rectangles in the XZ plane
module hull_segment(y1, w1, h1, y2, w2, h2, r=fillet_r) {
    hull() {
        translate([0, y1, 0])
            rotate([90, 0, 0])
                linear_extrude(0.01)
                    rrect_2d(w1, h1, min(r, min(w1, h1)/2 - 0.1));
        translate([0, y2, 0])
            rotate([90, 0, 0])
                linear_extrude(0.01)
                    rrect_2d(w2, h2, min(r, min(w2, h2)/2 - 0.1));
    }
}

// Create an ear hook curve from a starting point
module ear_hook(start_y, start_w, start_h, end_w, end_h, arc_len, angle, steps, r=fillet_r) {
    step_angle = angle / steps;
    step_len   = arc_len / steps;
    
    for (i = [0 : steps - 1]) {
        a1 = i * step_angle;
        a2 = (i + 1) * step_angle;
        
        // Interpolate width and height
        t1 = i / steps;
        t2 = (i + 1) / steps;
        w1 = start_w * (1 - t1) + end_w * t1;
        h1 = start_h * (1 - t1) + end_h * t1;
        w2 = start_w * (1 - t2) + end_w * t2;
        h2 = start_h * (1 - t2) + end_h * t2;
        
        // Compute positions along the arc (approximated)
        dy1 = cos(a1) * step_len * i;
        dz1 = -sin(a1) * step_len * i;
        dy2 = cos(a2) * step_len * (i + 1);
        dz2 = -sin(a2) * step_len * (i + 1);
        
        hull() {
            translate([0, start_y + dy1, dz1])
                rotate([a1, 0, 0])
                rotate([90, 0, 0])
                    linear_extrude(0.01)
                        rrect_2d(w1, h1, min(r, min(w1, h1)/2 - 0.1));
            translate([0, start_y + dy2, dz2])
                rotate([a2, 0, 0])
                rotate([90, 0, 0])
                    linear_extrude(0.01)
                        rrect_2d(w2, h2, min(r, min(w2, h2)/2 - 0.1));
        }
    }
}

// Single hinge barrel (cylinder with bore)
module hinge_barrel(od, id, h) {
    difference() {
        cylinder(d=od, h=h, $fn=max(30, $fn));
        translate([0, 0, -0.1])
            cylinder(d=id, h=h + 0.2, $fn=max(30, $fn));
    }
}


// ========================= FRONT FRAME ===============================

module front_frame() {
    difference() {
        union() {
            // Main frame body: extruded 2D profile
            translate([0, 0, 0])
            rotate([90, 0, 0])
            translate([0, 0, -frame_depth/2])
            linear_extrude(frame_depth) {
                difference() {
                    // Outer frame outline with rounded corners
                    hull() {
                        // Top-left corner
                        translate([-frame_total_w/2 + 8, frame_h/2 - 8])
                            circle(r=8);
                        // Top-right corner
                        translate([frame_total_w/2 - 8, frame_h/2 - 8])
                            circle(r=8);
                        // Bottom-left corner
                        translate([-frame_total_w/2 + 6, -frame_h/2 + 6])
                            circle(r=6);
                        // Bottom-right corner
                        translate([frame_total_w/2 - 6, -frame_h/2 + 6])
                            circle(r=6);
                    }
                    
                    // Right lens opening (elliptical)
                    translate([lens_cx, 0])
                        resize([lens_w, lens_h])
                            circle(d=1, $fn=80);
                    
                    // Left lens opening (elliptical, mirrored)
                    translate([-lens_cx, 0])
                        resize([lens_w, lens_h])
                            circle(d=1, $fn=80);
                }
            }
            
            // Frame-side hinge barrels — RIGHT side
            translate([hinge_x, 0, 0])
                right_frame_hinge_barrels();
            
            // Frame-side hinge barrels — LEFT side
            translate([-hinge_x, 0, 0])
                left_frame_hinge_barrels();
        }
        
        // === SUBTRACTIVE FEATURES ===
        
        // Wire channel groove along the top surface
        translate([0, 0, frame_h/2 - wire_ch_d/2 + 0.5])
            cube([frame_total_w - 30, wire_ch_w, wire_ch_d + 1], center=true);
        
        // Camera lens window (right side, near right hinge)
        // Camera faces forward (−Y direction)
        translate([frame_total_w/2 - rim_w - 3, -frame_depth/2 - 0.1, frame_h/2 - 10])
            rotate([-90, 0, 0])
                cylinder(d=cam_window_d, h=frame_depth + 0.2, $fn=40);
        
        // Nose pad sculpting — two angled cylindrical depressions on back face
        translate([5, frame_depth/2 - 1.5, -frame_h/2 + 14])
            rotate([10, -8, 0])
                cylinder(d=10, h=6, center=true, $fn=30);
        translate([-5, frame_depth/2 - 1.5, -frame_h/2 + 14])
            rotate([10, 8, 0])
                cylinder(d=10, h=6, center=true, $fn=30);
    }
    
    // Nose pad nubs (additive, on back face of bridge)
    translate([5, frame_depth/2 + 1.5, -frame_h/2 + 10])
        rotate([15, -10, 0])
            scale([1, 0.5, 1])
                sphere(d=6, $fn=20);
    translate([-5, frame_depth/2 + 1.5, -frame_h/2 + 10])
        rotate([15, 10, 0])
            scale([1, 0.5, 1])
                sphere(d=6, $fn=20);
}

// Frame-side hinge barrels (2 barrels with gap for temple barrel)
module right_frame_hinge_barrels() {
    arm_offset = frame_depth/2;
    
    // Connecting arm from frame edge to barrel axis
    hull() {
        translate([0, arm_offset - 1, 0])
            cube([2, 2, hinge_total_h], center=true);
        translate([hinge_arm_len, arm_offset, 0])
            cylinder(d=hinge_od, h=hinge_total_h, center=true, $fn=30);
    }
    
    // Top barrel
    translate([hinge_arm_len, arm_offset, hinge_temple_bh/2 + gap/2])
        hinge_barrel(hinge_od, hinge_id, hinge_frame_bh);
    
    // Bottom barrel
    translate([hinge_arm_len, arm_offset, -hinge_temple_bh/2 - gap/2 - hinge_frame_bh])
        hinge_barrel(hinge_od, hinge_id, hinge_frame_bh);
}

module left_frame_hinge_barrels() {
    mirror([1, 0, 0]) right_frame_hinge_barrels();
}


// ======================= RIGHT TEMPLE ================================
// Logic & Sensor Arm
// Zone A (0-26mm):  XIAO ESP32S3 Sense (transverse)
// Zone B (26-42mm): INMP441 MEMS Microphone
// Zone C (42-60mm): TTP223 Touch Sensor (outer wall)
// Zone D (60-85mm): MAX98357A Amp + Micro Speaker

module right_temple() {
    difference() {
        union() {
            right_temple_shell();
            right_temple_hinge_barrel();
        }
        right_temple_cavities();
        right_temple_ports();
    }
}

// Outer shell: hulled cross-section segments + ear hook
module right_temple_shell() {
    n = len(rt_sections);
    // Main straight segments
    for (i = [0 : n - 2]) {
        hull_segment(
            rt_sections[i][0],  rt_sections[i][1],  rt_sections[i][2],
            rt_sections[i+1][0], rt_sections[i+1][1], rt_sections[i+1][2]
        );
    }
    // Ear hook curve
    ear_hook(
        rt_sections[n-1][0], rt_sections[n-1][1], rt_sections[n-1][2],
        ear_hook_end_w, ear_hook_end_h,
        ear_hook_len, ear_hook_angle, ear_hook_steps
    );
}

// Temple-side hinge barrel (single central barrel)
module right_temple_hinge_barrel() {
    // Position: at Y=0 (front of temple), extending toward frame
    translate([0, -hinge_arm_len, 0]) {
        // Central barrel
        translate([0, 0, -hinge_temple_bh/2])
            hinge_barrel(hinge_od, hinge_id, hinge_temple_bh);
        
        // Connecting arm back to temple body
        hull() {
            cylinder(d=hinge_od, h=hinge_temple_bh, center=true, $fn=30);
            translate([0, hinge_arm_len - 1, 0])
                cube([hinge_od, 2, hinge_temple_bh], center=true);
        }
    }
}

// Internal component cavities (with clearance)
module right_temple_cavities() {
    c = tol;  // clearance per side
    
    // ---- Zone A: XIAO ESP32S3 Sense Cavity ----
    // Transverse mount: PCB width (17.8) along Y, PCB length (21) along Z
    // Camera at Y=0 face (forward), USB-C at bottom (−Z), headers extend in +Y
    xiao_cavity_y = 2;  // Start 2mm from front face (leave wall for camera window)
    translate([0, xiao_cavity_y + (xiao_h_tot + c)/2, 0])
        cube([xiao_w + 2*c, xiao_h_tot + 2*c, xiao_l + 2*c], center=true);
    
    // ---- Zone B: INMP441 Microphone Cavity ----
    inmp_y_center = 34;  // Center of INMP441 zone
    translate([0, inmp_y_center, -1])
        cube([inmp_w + 2*c, inmp_l + 2*c, inmp_h + 2*c], center=true);
    
    // ---- Zone C: TTP223 Touch Sensor Cavity ----
    // Mounted flat against outer wall (positive X face)
    ttp_y_center = 51;
    ttp_x_offset = (rt_sections[3][1])/2 - wall - ttp_h/2;  // Pushed to outer wall
    translate([ttp_x_offset, ttp_y_center, 0])
        cube([ttp_h + 2*c, ttp_l + 2*c, ttp_w + 2*c], center=true);
    
    // ---- Zone D: MAX98357A Amplifier Cavity ----
    max_y_center = 70;
    translate([0, max_y_center, 1])
        cube([max_w + 2*c, max_l + 2*c, max_h + 2*c], center=true);
    
    // ---- Zone D: Micro Speaker Cavity (below/behind amp) ----
    spk_y_center = 80;
    translate([0, spk_y_center, -rt_sections[4][2]/2 + wall + spk_h/2])
        cylinder(d=spk_d + 2*c, h=spk_h + 2*c, center=true, $fn=40);
    
    // ---- Internal wire routing tunnel ----
    // Continuous tunnel along the top interior for magnet wire
    translate([0, 50, rt_sections[2][2]/2 - wall - 1.5])
        cube([4, 80, 3], center=true);
}

// External port cutouts (through walls)
module right_temple_ports() {
    c = tol;
    extra = 4;  // Extra depth to ensure clean cuts through walls
    
    // ---- XIAO USB-C Port (faces DOWN, −Z) ----
    // Centered on X=0 at front of temple
    usbc_y = 2 + xiao_h_pcb/2;  // Y position of USB-C center
    translate([0, usbc_y, -rt_sections[0][2]/2 - 0.5])
        cube([xiao_usbc_w + 2*c, xiao_usbc_h + 4, extra], center=true);
    
    // ---- Camera Window (faces FORWARD, −Y from front face of temple) ----
    translate([0, -0.5, rt_sections[0][2]/2 - xiao_l/2 + 3])
        rotate([90, 0, 0])
            cylinder(d=cam_window_d, h=extra, center=true, $fn=30);
    
    // ---- INMP441 Mic Sound Port (faces DOWN, −Z) ----
    inmp_y_center = 34;
    translate([0, inmp_y_center, -rt_sections[2][2]/2 - 0.5])
        cylinder(d=inmp_port_d + 1, h=extra, center=true, $fn=20);
    
    // ---- Speaker Sound Port Array (faces DOWN, −Z toward ear) ----
    spk_y_center = 80;
    translate([0, spk_y_center, -rt_sections[4][2]/2 - 0.5]) {
        // Central port
        cylinder(d=3, h=extra, center=true, $fn=16);
        // Ring of smaller ports for acoustic dispersion
        for (a = [0 : 60 : 300]) {
            translate([cos(a)*4, sin(a)*4, 0])
                cylinder(d=2, h=extra, center=true, $fn=12);
        }
    }
    
    // ---- TTP223 Touch Pad Window (outer wall, +X face) ----
    ttp_y_center = 51;
    translate([rt_sections[3][1]/2 + 0.5, ttp_y_center, 0])
        cube([extra, 12, 11], center=true);
}


// ======================== LEFT TEMPLE ================================
// Power System Arm
// Zone A (0-14mm):  SS12D00 Slider Switch (bottom edge)
// Zone B (14-42mm): 160mAh LiPo Battery (pouch cell)
// Zone C (42-72mm): TP4056 Type-C Charging Module

module left_temple() {
    difference() {
        union() {
            left_temple_shell();
            left_temple_hinge_barrel();
        }
        left_temple_cavities();
        left_temple_ports();
    }
}

module left_temple_shell() {
    n = len(lt_sections);
    for (i = [0 : n - 2]) {
        hull_segment(
            lt_sections[i][0],  lt_sections[i][1],  lt_sections[i][2],
            lt_sections[i+1][0], lt_sections[i+1][1], lt_sections[i+1][2]
        );
    }
    // Ear hook
    ear_hook(
        lt_sections[n-1][0], lt_sections[n-1][1], lt_sections[n-1][2],
        ear_hook_end_w, ear_hook_end_h,
        ear_hook_len, ear_hook_angle, ear_hook_steps
    );
}

module left_temple_hinge_barrel() {
    translate([0, -hinge_arm_len, 0]) {
        translate([0, 0, -hinge_temple_bh/2])
            hinge_barrel(hinge_od, hinge_id, hinge_temple_bh);
        hull() {
            cylinder(d=hinge_od, h=hinge_temple_bh, center=true, $fn=30);
            translate([0, hinge_arm_len - 1, 0])
                cube([hinge_od, 2, hinge_temple_bh], center=true);
        }
    }
}

module left_temple_cavities() {
    c = tol;
    
    // ---- Zone A: Slider Switch Cavity ----
    // Bottom-mounted, knob protrudes downward
    sw_y_center = 7;
    translate([0, sw_y_center, -lt_sections[0][2]/2 + wall + sw_body_h/2])
        cube([sw_l + 2*c, sw_w + 2*c, sw_body_h + 2*c], center=true);
    
    // ---- Zone B: 160mAh LiPo Battery Cavity ----
    bat_y_center = 28;
    translate([0, bat_y_center, 0])
        cube([bat_w + 2*c, bat_l + 2*c, bat_h + 2*c], center=true);
    
    // ---- Zone C: TP4056 Charger Module Cavity ----
    tp_y_center = 57;
    translate([0, tp_y_center, -1])
        cube([tp_w + 2*c, tp_l + 2*c, tp_h + 2*c], center=true);
    
    // ---- Internal wire routing tunnel ----
    translate([0, 40, lt_sections[2][2]/2 - wall - 1.5])
        cube([4, 65, 3], center=true);
}

module left_temple_ports() {
    c = tol;
    extra = 4;
    
    // ---- Slider Switch Knob Slot (faces DOWN, −Z) ----
    sw_y_center = 7;
    translate([0, sw_y_center, -lt_sections[0][2]/2 - 0.5])
        cube([sw_l - 1, sw_w + 1, extra], center=true);
    
    // ---- TP4056 USB-C Port (faces DOWN, −Z) ----
    tp_y_center = 57;
    translate([0, tp_y_center - tp_l/2 + 4, -lt_sections[3][2]/2 - 0.5])
        cube([tp_usbc_w + 2*c, tp_usbc_h + 3, extra], center=true);
    
    // ---- TP4056 LED indicator window (tiny slot on outer wall) ----
    translate([lt_sections[3][1]/2 + 0.5, tp_y_center, -2])
        cube([extra, 4, 2], center=true);
}


// ========================= ASSEMBLY ==================================
// Full assembled view with temples in "open" (worn) position
// Temples extend backward (in +Y from frame hinge points)

module full_assembly() {
    color("SlateGray", 0.85)
        front_frame();
    
    // Right temple: translate to right hinge, rotate open 90°
    translate([hinge_x + hinge_arm_len, frame_depth/2, hinge_z])
        rotate([0, 0, 90])  // Swing open (temple extends in +Y)
            color("DimGray", 0.85)
                right_temple();
    
    // Left temple: mirror on X, translate to left hinge, rotate open 90°
    translate([-(hinge_x + hinge_arm_len), frame_depth/2, hinge_z])
        rotate([0, 0, -90])
            color("DimGray", 0.85)
                mirror([1, 0, 0])
                    left_temple();
}


// ==================== INDIVIDUAL PART EXPORTS ========================
// Uncomment ONE of the following to export individual STL parts
// in their optimal print orientation

module export_front_frame() {
    // Print flat: XY on bed, Z up (as designed)
    front_frame();
}

module export_right_temple() {
    // Print on inner face: rotate so flat inner wall faces bed
    rotate([90, 0, 0])
        right_temple();
}

module export_left_temple() {
    rotate([90, 0, 0])
        left_temple();
}


// ========================= RENDER ====================================
// Default: Show full assembly
// Toggle comments below to switch between assembly view and individual parts

full_assembly();
// export_front_frame();
// export_right_temple();
// export_left_temple();


// ======================= COMPONENT GHOSTS ============================
// Uncomment to visualize component placement inside right temple
// (Semi-transparent red blocks showing exact cavity positions)

module show_right_temple_components() {
    // Requires right temple to be positioned at origin
    c = tol;
    
    // XIAO ESP32S3 Sense
    color("Red", 0.3)
    translate([0, 2 + (xiao_h_tot + c)/2, 0])
        cube([xiao_w, xiao_h_tot, xiao_l], center=true);
    
    // INMP441
    color("Blue", 0.3)
    translate([0, 34, -1])
        cube([inmp_w, inmp_l, inmp_h], center=true);
    
    // TTP223
    color("Green", 0.3) {
        ttp_x_offset = (rt_sections[3][1])/2 - wall - ttp_h/2;
        translate([ttp_x_offset, 51, 0])
            cube([ttp_h, ttp_l, ttp_w], center=true);
    }
    
    // MAX98357A
    color("Orange", 0.3)
    translate([0, 70, 1])
        cube([max_w, max_l, max_h], center=true);
    
    // Speaker
    color("Purple", 0.3)
    translate([0, 80, -rt_sections[4][2]/2 + wall + spk_h/2])
        cylinder(d=spk_d, h=spk_h, center=true, $fn=30);
}

module show_left_temple_components() {
    c = tol;
    
    // Slider Switch
    color("Yellow", 0.3)
    translate([0, 7, -lt_sections[0][2]/2 + wall + sw_body_h/2])
        cube([sw_l, sw_w, sw_body_h], center=true);
    
    // LiPo Battery
    color("Red", 0.3)
    translate([0, 28, 0])
        cube([bat_w, bat_l, bat_h], center=true);
    
    // TP4056
    color("Blue", 0.3)
    translate([0, 57, -1])
        cube([tp_w, tp_l, tp_h], center=true);
}

// Uncomment to show component ghosts in assembly context:
// translate([hinge_x + hinge_arm_len, frame_depth/2, hinge_z])
//     rotate([0, 0, 90])
//         show_right_temple_components();
// translate([-(hinge_x + hinge_arm_len), frame_depth/2, hinge_z])
//     rotate([0, 0, -90])
//         mirror([1, 0, 0])
//             show_left_temple_components();
