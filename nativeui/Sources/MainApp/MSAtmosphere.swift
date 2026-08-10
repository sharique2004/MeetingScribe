// The Aurora atmosphere: every optical device the shell is allowed to use,
// in one file, so the budget stays visible. The sky (MSAuroraField) exists
// in three windows — sidebar, the Today header band, the live page — and
// nowhere else. The beam (MSBeam) has exactly two homes, declared at its
// call sites. Edge light replaces borders on raised chrome. Grain is a
// static image, generated once. Reading surfaces get none of this: the
// document is a window cut through the weather, not a participant in it.
import SwiftUI
import AppKit

// MARK: - Elevation

/// Four-layer shadow ramps: blur doubling, opacity decaying, always tinted
/// MS.shadowTint (a violet world casts violet shadows). Never on list rows —
/// four offscreen passes per row is a price a 200-row library cannot pay.
enum MSElevation {
    case raised, floating, overlay

    /// (blur, y) per layer; alphas per scheme.
    var layers: [(blur: CGFloat, y: CGFloat)] {
        switch self {
        case .raised:   [(2, 1), (4, 2), (8, 4), (16, 8)]
        case .floating: [(4, 2), (8, 4), (16, 8), (32, 16)]
        case .overlay:  [(6, 3), (12, 6), (24, 12), (48, 24)]
        }
    }

    func alphas(dark: Bool) -> [Double] {
        switch self {
        case .raised:   dark ? [0.20, 0.15, 0.11, 0.07] : [0.05, 0.04, 0.03, 0.02]
        case .floating: dark ? [0.26, 0.20, 0.14, 0.09] : [0.07, 0.055, 0.04, 0.03]
        case .overlay:  dark ? [0.30, 0.22, 0.16, 0.10] : [0.10, 0.08, 0.06, 0.04]
        }
    }
}

private struct MSElevationModifier: ViewModifier {
    @Environment(\.colorScheme) private var scheme
    let tier: MSElevation

    func body(content: Content) -> some View {
        let alphas = tier.alphas(dark: scheme == .dark)
        return tier.layers.indices.reduce(AnyView(content)) { view, i in
            AnyView(view.shadow(color: MS.shadowTint.opacity(alphas[i]),
                                radius: tier.layers[i].blur,
                                y: tier.layers[i].y))
        }
    }
}

// MARK: - Edge light

/// The 1px lit edge that replaces borders on raised chrome: brightest at the
/// top, falling to almost nothing at the bottom. In light mode the gradient
/// carries a specular top catch and a violet contact line below — the same
/// sun, inverted. Under Increase Contrast the tokens themselves go opaque.
private struct MSEdgeLit: ViewModifier {
    let radius: CGFloat

    func body(content: Content) -> some View {
        content.overlay(
            MS.rr(radius).strokeBorder(
                LinearGradient(colors: [MS.edgeTop, MS.edgeBottom],
                               startPoint: .top, endPoint: .bottom),
                lineWidth: 1)
            .allowsHitTesting(false))
    }
}

extension View {
    func msElevation(_ tier: MSElevation) -> some View {
        modifier(MSElevationModifier(tier: tier))
    }

    /// Fill + lit edge in one move: the standard treatment for a raised card.
    func msSurface(_ surface: Color, radius: CGFloat,
                   elevation: MSElevation? = nil) -> some View {
        self
            .background(surface, in: MS.rr(radius))
            .modifier(MSEdgeLit(radius: radius))
            .modifier(MSOptionalElevation(tier: elevation))
    }

    /// The lit edge alone — for glass, which brings its own fill.
    func msEdgeLit(radius: CGFloat) -> some View {
        modifier(MSEdgeLit(radius: radius))
    }

    /// The uniform contact rim, for shapes the gradient stroke would overdress
    /// (the HUD capsule over an arbitrary desktop).
    func msRim<S: InsettableShape>(_ shape: S) -> some View {
        overlay(shape.strokeBorder(MS.edgeRim, lineWidth: 1)
            .allowsHitTesting(false))
    }
}

private struct MSOptionalElevation: ViewModifier {
    let tier: MSElevation?

    func body(content: Content) -> some View {
        if let tier { content.msElevation(tier) } else { content }
    }
}

// MARK: - Grain

/// Photographic tooth for the shell. Generated once, from a fixed seed, into
/// a 128² tile — no asset, no build step, zero per-frame cost. White noise
/// on the night; ink noise on paper (white noise on a light surface reads as
/// dirt, dark noise reads as tooth). Off under Reduce Transparency, where
/// texture is exactly what was asked to go away.
enum MSGrain {
    static let tile: CGImage = {
        let size = 128
        var rng: UInt64 = 0x5EEA_D4A0_44B0_1013   // fixed seed: same grain every launch
        func next() -> UInt8 {
            rng = rng &* 6364136223846793005 &+ 1442695040888963407
            return UInt8(truncatingIfNeeded: rng >> 33)
        }
        var pixels = [UInt8](repeating: 0, count: size * size)
        for i in pixels.indices { pixels[i] = next() }
        let data = CFDataCreate(nil, pixels, pixels.count)!
        let provider = CGDataProvider(data: data)!
        return CGImage(width: size, height: size, bitsPerComponent: 8,
                       bitsPerPixel: 8, bytesPerRow: size,
                       space: CGColorSpaceCreateDeviceGray(),
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                       provider: provider, decode: nil,
                       shouldInterpolate: false, intent: .defaultIntent)!
    }()
}

private struct MSGrainOverlay: ViewModifier {
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    func body(content: Content) -> some View {
        content.overlay {
            if !reduceTransparency, contrast != .increased {
                Image(decorative: MSGrain.tile, scale: 1)
                    .resizable(resizingMode: .tile)
                    .opacity(scheme == .dark ? 0.025 : 0.016)
                    .blendMode(scheme == .dark ? .screen : .multiply)
                    .allowsHitTesting(false)
            }
        }
    }
}

extension View {
    func msGrain() -> some View { modifier(MSGrainOverlay()) }
}

// MARK: - The sky

/// One aurora, seen through up to three windows. Every instance derives its
/// drift from wall-clock time with the same mutually-prime periods, so two
/// fields on screen are always in phase — the sky is continuous across the
/// sidebar seam without any shared object.
///
/// Dose is the whole design: the mesh mixes the surface toward violet /
/// indigo / aqua by fractions chosen to land near 5% OKLCH chroma — weather,
/// not wallpaper. The axis runs violet → indigo → AQUA, which is what keeps
/// it out of textbook AI purple-blue: the working color is present in the
/// sky as a whisper (the web's "Aqua Does the Work" rule, inverted).
struct MSAuroraField: View {
    /// 0–1: how much of the field shows (Today runs it at 0.8, Live at 0.45).
    var strength: Double = 1

    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast
    @State private var appActive = true

    var body: some View {
        Group {
            if reduceTransparency || contrast == .increased {
                // The sky as a still, opaque wash: identity kept, optics gone.
                LinearGradient(colors: [flatTop, flatBottom],
                               startPoint: .top, endPoint: .bottom)
            } else if reduceMotion {
                mesh(at: 0)   // frozen at dawn, not deleted — it IS the identity
            } else {
                TimelineView(.animation(minimumInterval: 1.0 / 20, paused: !appActive)) { context in
                    mesh(at: context.date.timeIntervalSinceReferenceDate)
                }
            }
        }
        .opacity(strength)
        .allowsHitTesting(false)
        .onReceive(NotificationCenter.default.publisher(
            for: NSApplication.didResignActiveNotification)) { _ in appActive = false }
        .onReceive(NotificationCenter.default.publisher(
            for: NSApplication.didBecomeActiveNotification)) { _ in appActive = true }
    }

    private func mesh(at t: TimeInterval) -> some View {
        // Mutually prime periods: the sky never visibly loops.
        let a = Float(sin(t * 2 * .pi / 41) * 0.06)
        let b = Float(cos(t * 2 * .pi / 53) * 0.06)
        let c = Float(sin(t * 2 * .pi / 67) * 0.06)
        return MeshGradient(
            width: 3, height: 3,
            points: [
                [0, 0], [0.5 + a, 0], [1, 0],
                [0, 0.5 + b], [0.5 + c, 0.5 - a], [1, 0.5 - b],
                [0, 1], [0.5 - c, 1], [1, 1],
            ],
            colors: colors)
        .blur(radius: 40)
    }

    /// Explicit per-scheme colors — MeshGradient gets resolved values, never
    /// appearance-dynamic ones, so the dose is exact and deterministic.
    private var colors: [Color] {
        if scheme == .dark {
            let base = "#0C0B13"
            return [
                Color(hex: mix(base, "#7D5CDF", 0.30)),  // violet crown, top-left
                Color(hex: mix(base, "#2E63D8", 0.16)),
                Color(hex: mix(base, "#7D5CDF", 0.12)),
                Color(hex: mix(base, "#2E63D8", 0.22)),  // indigo mid — transition, never an endpoint
                Color(hex: mix(base, "#7D5CDF", 0.10)),
                Color(hex: mix(base, "#5EEAD4", 0.14)),  // the aqua whisper, one region
                Color(hex: base),
                Color(hex: mix(base, base, 0)),
                Color(hex: mix(base, "#5EEAD4", 0.06)),
            ]
        } else {
            let base = "#EEECF3"
            return [
                Color(hex: "#F0EAFB"), Color(hex: "#EAEDFA"), Color(hex: "#F0EAFB"),
                Color(hex: "#E8EFFB"), Color(hex: base), Color(hex: "#E6FAF5"),
                Color(hex: base), Color(hex: base), Color(hex: "#EBF8F4"),
            ]
        }
    }

    private var flatTop: Color {
        scheme == .dark ? Color(hex: mix("#0C0B13", "#7D5CDF", 0.10))
                        : Color(hex: "#F0EAFB")
    }
    private var flatBottom: Color {
        scheme == .dark ? Color(hex: "#0C0B13") : Color(hex: "#EEECF3")
    }

    private func mix(_ from: String, _ to: String, _ f: Double) -> String {
        func rgb(_ s: String) -> (Double, Double, Double) {
            var h = s; if h.hasPrefix("#") { h.removeFirst() }
            var v: UInt64 = 0
            Scanner(string: h).scanHexInt64(&v)
            return (Double((v >> 16) & 0xFF), Double((v >> 8) & 0xFF), Double(v & 0xFF))
        }
        let (r1, g1, b1) = rgb(from), (r2, g2, b2) = rgb(to)
        return String(format: "#%02X%02X%02X",
                      Int(r1 + (r2 - r1) * f), Int(g1 + (g2 - g1) * f),
                      Int(b1 + (b2 - b1) * f))
    }
}

// MARK: - The beam

/// The one recurring signature, straight from the web system: a thin arc of
/// aqua with a near-white hot core and a sky tail, orbiting a border, with a
/// blurred twin breathing behind it. Two homes — the armed record button and
/// the playing transcript line — and never a third. Reduce Motion freezes it
/// to the web's own fallback: a static arc from 130°.
struct MSBeam: View {
    var radius: CGFloat
    var period: Double = 7
    var lineWidth: CGFloat = 1.5

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorSchemeContrast) private var contrast

    // The web's exact stops (templates/index.html:226): transparent to 68%,
    // aqua 80%, #d6fff6 hot core 88%, sky 94%, gone.
    private var gradient: AngularGradient {
        AngularGradient(stops: [
            .init(color: .clear, location: 0),
            .init(color: .clear, location: 0.68),
            .init(color: Color(hex: "#5EEAD4").opacity(0.9), location: 0.80),
            .init(color: Color(hex: "#D6FFF6"), location: 0.88),
            .init(color: Color(hex: "#7DD3FC").opacity(0.9), location: 0.94),
            .init(color: .clear, location: 1),
        ], center: .center)
    }

    var body: some View {
        if reduceMotion {
            ring(rotation: .degrees(130))
        } else {
            TimelineView(.animation(minimumInterval: 1.0 / 30)) { context in
                let t = context.date.timeIntervalSinceReferenceDate
                let angle = Angle.degrees((t.truncatingRemainder(dividingBy: period)) / period * 360)
                ZStack {
                    if contrast != .increased {
                        ring(rotation: angle).blur(radius: 9).opacity(0.45)
                    }
                    ring(rotation: angle)
                }
            }
        }
    }

    private func ring(rotation: Angle) -> some View {
        gradient
            .rotationEffect(rotation)
            .mask(MS.rr(radius).strokeBorder(lineWidth: lineWidth))
            .allowsHitTesting(false)
    }
}

// MARK: - Vignettes and fades

extension View {
    /// The waveform's dissolve: edges melt into the surrounding surface
    /// instead of ending in a box. A scrim in the surface color plus a
    /// horizontal end-fade — no blend modes, no shaders, scheme-safe.
    func msVignette(in surface: Color) -> some View {
        self
            .overlay(
                EllipticalGradient(colors: [.clear, surface.opacity(0.5)],
                                   center: .center,
                                   startRadiusFraction: 0.55,
                                   endRadiusFraction: 1)
                .allowsHitTesting(false))
            .mask(
                LinearGradient(stops: [
                    .init(color: .clear, location: 0),
                    .init(color: .black, location: 0.06),
                    .init(color: .black, location: 0.94),
                    .init(color: .clear, location: 1),
                ], startPoint: .leading, endPoint: .trailing))
    }

    /// The bottom of a reading scroll melts into its surface instead of being
    /// guillotined by the transport shelf. A scrim, not a mask, so it needs no
    /// knowledge of what scrolls underneath.
    func msBottomFade(_ surface: Color, height: CGFloat = 86) -> some View {
        overlay(alignment: .bottom) {
            LinearGradient(colors: [.clear, surface],
                           startPoint: .top, endPoint: .bottom)
                .frame(height: height)
                .allowsHitTesting(false)
        }
    }
}
