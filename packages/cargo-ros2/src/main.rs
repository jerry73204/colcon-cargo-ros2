use cargo_ros2::{BindgenConfig, InstallConfig};
use clap::{Parser, Subcommand};
use eyre::Result;
use std::env;
use std::path::PathBuf;

/// All-in-one build tool for ROS 2 Rust projects
#[derive(Parser, Debug)]
#[command(name = "cargo")]
#[command(bin_name = "cargo")]
enum CargoCli {
    Ros2(Ros2Args),
}

#[derive(Debug, Parser)]
#[command(name = "ros2")]
#[command(about = "Build tool for ROS 2 Rust projects", long_about = None)]
struct Ros2Args {
    #[command(subcommand)]
    command: Ros2Command,

    /// Verbose output
    #[arg(short, long, global = true)]
    verbose: bool,
}

#[derive(Debug, Subcommand)]
enum Ros2Command {
    /// Generate Rust bindings for a ROS 2 interface package
    Bindgen {
        /// ROS package name
        #[arg(long)]
        package: String,

        /// Output directory for generated bindings
        #[arg(long)]
        output: PathBuf,

        /// Direct path to package share directory (bypasses ament index)
        #[arg(long)]
        package_path: Option<PathBuf>,

        /// Verbose output
        #[arg(long)]
        verbose: bool,

        /// Stamp the crate with the ROS package version instead of a fixed one
        ///
        /// Needed when a consumer requires a version of this crate rather than
        /// `*`: a [patch.crates-io] entry still has to satisfy that
        /// requirement, and the fixed version cannot.
        #[arg(long)]
        use_ros_package_version: bool,
    },

    /// Install binaries and libraries to ament layout
    Install {
        /// Install base directory (install/<package>/)
        #[arg(long)]
        install_base: PathBuf,

        /// Workspace build directory
        #[arg(long)]
        build_base: Option<PathBuf>,

        /// Build profile (debug or release)
        #[arg(long, default_value = "debug")]
        profile: String,

        /// Target triple the build was compiled for (cross-compilation)
        #[arg(long)]
        target: Option<String>,

        /// Features that were enabled during compilation
        #[arg(long, value_delimiter = ',')]
        features: Vec<String>,

        /// The build ran with --no-default-features
        #[arg(long)]
        no_default_features: bool,

        /// The build ran with --all-features
        #[arg(long)]
        all_features: bool,
    },

    /// Clean generated bindings and cache
    Clean,

    /// Diagnose why a plain `cargo` invocation fails in this workspace
    Doctor {
        /// Crate directory to diagnose (defaults to the current directory)
        #[arg(long)]
        path: Option<PathBuf>,
    },
}

fn main() -> Result<()> {
    let CargoCli::Ros2(args) = CargoCli::parse();

    // Get project root (current directory)
    let project_root = env::current_dir()?;

    match args.command {
        Ros2Command::Bindgen {
            package,
            output,
            package_path,
            verbose,
            use_ros_package_version,
        } => {
            let config = BindgenConfig {
                package_name: package,
                package_path,
                output_dir: output,
                verbose: verbose || args.verbose,
                rosidl_runtime_rs_version: None,
                use_ros_package_version,
            };
            cargo_ros2::generate_bindings(config)?;
            println!("✓ Bindings generated successfully");
        }

        Ros2Command::Install {
            install_base,
            build_base,
            profile,
            target,
            features,
            no_default_features,
            all_features,
        } => {
            // Default build_base to project_root if not provided
            let build_base = build_base.unwrap_or_else(|| project_root.clone());

            let config = InstallConfig {
                project_root,
                install_base,
                build_base,
                profile,
                verbose: args.verbose,
                arch: target,
                features,
                no_default_features,
                all_features,
            };
            cargo_ros2::install_to_ament(config)?;
            println!("✓ Package installed successfully");
        }

        Ros2Command::Clean => {
            cargo_ros2::clean_bindings(&project_root, args.verbose)?;
            println!("✓ Cleaned bindings and cache!");
        }

        Ros2Command::Doctor { path } => {
            let crate_dir = path.unwrap_or(project_root);
            if !cargo_ros2::doctor::run(&crate_dir)? {
                // Exit non-zero so scripts and CI can gate on a healthy workspace.
                std::process::exit(1);
            }
            println!("✓ Workspace looks healthy");
        }
    }

    Ok(())
}
