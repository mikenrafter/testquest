{
  description = "testquest: tq finds tests that cannot fail, replays the faults they name, and keeps a ledger of what the suite catches";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs:
        let
          tq = pkgs.stdenvNoCC.mkDerivation {
            pname = "tq";
            version = "0.1.0";
            src = pkgs.lib.fileset.toSource {
              root = ./.;
              fileset = pkgs.lib.fileset.unions [ ./tq ./tq_rules ./props ./tests ];
            };
            nativeBuildInputs = [ pkgs.makeWrapper ];
            nativeCheckInputs = [ pkgs.python3 pkgs.git ];
            doCheck = true;
            checkPhase = ''
              runHook preCheck
              export HOME=$TMPDIR
              git config --global user.name check
              git config --global user.email check@localhost
              python3 -I -m unittest discover -s tests
              runHook postCheck
            '';
            installPhase = ''
              runHook preInstall
              mkdir -p $out/share/testquest $out/bin
              cp -r tq tq_rules props $out/share/testquest/
              makeWrapper ${pkgs.python3}/bin/python3 $out/bin/tq \
                --add-flags "-I $out/share/testquest/tq" \
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.git ]}
              runHook postInstall
            '';
            meta = {
              description = "Find tests that cannot fail, replay the faults they name, and keep a test ledger";
              homepage = "https://github.com/mikenrafter/testquest";
              license = pkgs.lib.licenses.mit;
              mainProgram = "tq";
            };
          };
        in
        {
          inherit tq;
          default = tq;
        });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell { packages = [ pkgs.python3 pkgs.git ]; };
      });
    };
}
