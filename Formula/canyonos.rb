class Canyonos < Formula
  desc "CLI for CanyonOS"
  homepage "https://github.com/CanyonCodeCoreAI/canyoncodecore"
  version "0.1.5"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/CanyonCodeCoreAI/canyoncodecore/releases/download/cli-v#{version}/canyonos-macos-arm64"
      sha256 "REPLACE_WITH_MACOS_ARM64_SHA256"
    else
      url "https://github.com/CanyonCodeCoreAI/canyoncodecore/releases/download/cli-v#{version}/canyonos-macos-x86_64"
      sha256 "REPLACE_WITH_MACOS_X86_64_SHA256"
    end
  end

  on_linux do
    url "https://github.com/CanyonCodeCoreAI/canyoncodecore/releases/download/cli-v#{version}/canyonos-linux-x86_64"
    sha256 "REPLACE_WITH_LINUX_X86_64_SHA256"
  end

  def install
    bin.install Dir["canyonos-*"].first => "canyonos"
  end

  test do
    system "#{bin}/canyonos", "version"
  end
end
