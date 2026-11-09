// Deploys the API to the GHG DataHub server.
//
// GitHub Actions already tests every change and promotes stage -> main; this job only
// deploys what is on `main`. Point the Jenkins job at `main` (a GitHub webhook on push
// triggers it, or run it by hand with "Build with Parameters").
//
// Credentials (Jenkins -> Manage credentials):
//   ghg-datahub       Username with password   SSH login on the server
//   ghg-datahub-host  Secret text              server IP / host name
def remote = [:]

pipeline {

    agent any

    parameters {
        // The schema must match the ORM that is installed. Untick it while the database is not
        // ready yet, or when it was set up by hand and has never been stamped (see README).
        booleanParam(name: 'RUN_MIGRATIONS', defaultValue: true,
                     description: 'Apply pending database migrations before restarting the API')
    }

    environment {
        // Username with password: Jenkins also exposes server_USR and server_PSW.
        server = credentials('ghg-datahub')
        host   = credentials('ghg-datahub-host')

        BRANCH    = 'main'
        APP_DIR   = '/opt/ghg-datahub/back'
        CONDA_SH  = '/opt/miniforge3/etc/profile.d/conda.sh'
        CONDA_ENV = 'mplsoildata-backend'
        PM2_APP   = 'mfl_api'
        NODE_ENV_NAME = 'mplsoildata-frontend'   // conda env that has node (pm2 needs it)
        API_PORT  = '8090'   // 8000 is taken by another app on this server
        // The API installs the shared ORM package from its own repository, same branch.
        ORM_REPO  = 'git+https://github.com/Dan3s/mflsoildata_orm@main'
    }

    stages {
        stage('Ssh to connect GHG DataHub server') {
            steps {
                script {
                    remote.allowAnyHosts = true
                    remote.user = server_USR
                    remote.password = server_PSW
                    remote.name = 'ghg-datahub'
                    remote.host = host
                }
            }
        }

        stage('Update code') {
            steps {
                script {
                    try {
                        // reset instead of pull: the server never keeps local edits, and a
                        // pull would stop on anything a previous run left modified.
                        sshCommand remote: remote, command: """
                            set -e
                            cd ${env.APP_DIR}
                            git fetch --prune origin
                            git checkout ${env.BRANCH}
                            git reset --hard origin/${env.BRANCH}
                        """
                    } catch (Exception e) {
                        echo "Git update error: ${e.message}"
                        error("Failed to update code: ${e.message}")
                    }
                }
            }
        }

        stage('Install dependencies') {
            steps {
                script {
                    sshCommand remote: remote, command: """
                        set -e
                        cd ${env.APP_DIR}
                        . ${env.CONDA_SH}
                        conda activate ${env.CONDA_ENV}
                        pip install -r requirements.txt
                        # The ORM asks for "psycopg2", which pip would compile from source and this
                        # server has no pg_config for that. psycopg2-binary (requirements.txt) already
                        # provides the same module, so the ORM goes in without its dependencies, plus
                        # the two it needs that requirements.txt does not list. It is always taken as it
                        # is on its branch (pip reinstalls VCS requirements).
                        pip install --upgrade --no-deps ${env.ORM_REPO}
                        pip install "alembic>=1.18.2" "geoalchemy2>=0.15.2"
                        # Fails here, not at start-up, if the ORM ever gains another dependency (the
                        # placeholder URL only lets the import run; nothing connects to it).
                        DATABASE_URL=postgresql://check:check@localhost:5432/check python -c "import mflsoildata_orm.models, mflsoildata_orm.migrations"
                    """
                }
            }
        }

        stage('Database migrations') {
            when { expression { params.RUN_MIGRATIONS != false } }
            steps {
                script {
                    // Runs before the restart: if a migration fails, the old API keeps serving.
                    sshCommand remote: remote, command: """
                        set -e
                        cd ${env.APP_DIR}
                        . ${env.CONDA_SH}
                        conda activate ${env.CONDA_ENV}
                        python -c "from dotenv import load_dotenv; load_dotenv('.env'); from mflsoildata_orm.migrations import upgrade, current; upgrade(); current()"
                    """
                }
            }
        }

        stage('Restart API') {
            steps {
                script {
                    sshCommand remote: remote, command: """
                        set -e
                        cd ${env.APP_DIR}
                        . ${env.CONDA_SH}
                        conda activate ${env.CONDA_ENV}
                        export MFL_UVICORN="\$(which uvicorn)"
                        export MFL_API_PORT=${env.API_PORT}
                        # pm2 is a Node program and this Python env has no node: borrow the one from
                        # the frontend env, only for the pm2 commands (uvicorn is already an absolute path).
                        if ! command -v node > /dev/null 2>&1; then
                            NODE_BIN="\$(conda run -n ${env.NODE_ENV_NAME} which node)"
                            export PATH="\$(dirname "\$NODE_BIN"):\$PATH"
                        fi
                        # Shared server: never start on, or "verify" against, a port someone else uses.
                        if ss -ltn | grep -q ":${env.API_PORT} " && ! pm2 describe ${env.PM2_APP} > /dev/null 2>&1; then
                            echo "Port ${env.API_PORT} is already used by another process" >&2
                            exit 1
                        fi
                        pm2 startOrRestart ecosystem.config.js --only ${env.PM2_APP} --update-env
                        pm2 save
                    """
                }
            }
        }

        stage('Check it is up') {
            steps {
                script {
                    sshCommand remote: remote, command: """
                        for i in 1 2 3 4 5 6 7 8 9 10; do
                            if curl -fsS http://127.0.0.1:${env.API_PORT}/health > /dev/null; then
                                echo "API is answering on /health"
                                exit 0
                            fi
                            sleep 3
                        done
                        echo "API did not answer on /health" >&2
                        pm2 logs ${env.PM2_APP} --lines 40 --nostream || true
                        exit 1
                    """
                }
            }
        }
    }

    post {
        failure {
            echo 'API deploy failed: check the stage above and `pm2 logs mfl_api` on the server'
        }
        success {
            echo 'API deployed'
        }
    }
}
