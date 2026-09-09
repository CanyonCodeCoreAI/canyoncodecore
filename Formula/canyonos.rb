class Canyonos < Formula
  desc "CLI for CanyonOS"
  homepage "https://github.com/CanyonCodeCoreAI/canyoncodecore"
  version "0.1.5"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/CanyonCodeCoreAI/canyoncodecore/releases/download/cli-v#{version}/canyonos-macos-arm64"
      sha256 "068136e3e69ea502b8651cb2ab889ee53a7efc5d35723dce85f91b04817bef76"
    else
      url "https://github.com/CanyonCodeCoreAI/canyoncodecore/releases/download/cli-v#{version}/canyonos-macos-x86_64"
      sha256 "REPLACE_WITH_MACOS_X86_64_SHA256"
    end
  end

  on_linux do
    url "https://github.com/CanyonCodeCoreAI/canyoncodecore/releases/download/cli-v#{version}/canyonos-linux-x86_64"
    sha256 "1b5add3432079d06b2ec055f4a0da8a65961a33a0fd2f57ca79787ff01ca243f"
  end

  def install
    bin.install Dir["canyonos-*"].first => "canyonos"
  end

  test do
    system "#{bin}/canyonos", "version"
  end
end
