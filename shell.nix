{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    python313
    ruff
    ty
    uv
  ];

  # Browsers for the Python playwright package (pinned to the same version
  # as pkgs.playwright-driver), used by natbank.py to download QFX files.
  shellHook = ''
    export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
    export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
    export LD_LIBRARY_PATH=${pkgs.stdenv.cc.cc.lib}/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
    export PLAYWRIGHT_NODEJS_PATH=${pkgs.nodejs}/bin/node
  '';
}
